"""Unit tests for modules/11_aurora. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

AuroraStack = stack_class("11_aurora")


def _synth(config):
    app = cdk.App()
    stack = AuroraStack(app, "TestAuroraStack", config=config)
    return Template.from_stack(stack)


def test_aurora_postgresql_serverless_v2_with_one_reader_by_default(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::RDS::DBCluster",
        {
            "Engine": "aurora-postgresql",
            "EngineVersion": "17.9",
            "DatabaseName": "app",
            "StorageEncrypted": True,
            "ServerlessV2ScalingConfiguration": {"MinCapacity": 0.5, "MaxCapacity": 4},
        },
    )
    template.resource_count_is("AWS::RDS::DBInstance", 2)
    template.has_resource_properties("AWS::RDS::DBInstance", {"DBInstanceClass": "db.serverless"})


def test_reader_count_and_provisioned_instances_are_configurable(config, monkeypatch):
    monkeypatch.setenv("CDK_AURORA_READERS", "3")
    monkeypatch.setenv("CDK_AURORA_INSTANCES", "provisioned")
    monkeypatch.setenv("CDK_AURORA_INSTANCE_TYPE", "r7g.large")
    template = _synth(config)
    template.resource_count_is("AWS::RDS::DBInstance", 4)
    template.has_resource_properties("AWS::RDS::DBInstance", {"DBInstanceClass": "db.r7g.large", "PromotionTier": 0})
    template.has_resource_properties("AWS::RDS::DBInstance", {"PromotionTier": 2})


def test_read_requests_go_to_the_reader_service(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        {
            "Priority": 10,
            "Conditions": [
                Match.object_like(
                    {"Field": "http-request-method", "HttpRequestMethodConfig": {"Values": ["GET", "HEAD"]}}
                )
            ],
        },
    )
    template.resource_count_is("AWS::ElasticLoadBalancingV2::TargetGroup", 2)


def test_write_api_uses_the_writer_endpoint_and_read_api_the_reader_endpoint(config):
    template = _synth(config)
    uris = {}
    for resource in template.find_resources("AWS::ECS::TaskDefinition").values():
        container = resource["Properties"]["ContainerDefinitions"][0]
        for env in container.get("Environment", []):
            if env["Name"] == "PGRST_DB_URI":
                uris[resource["Properties"]["Family"]] = str(env["Value"])
    assert "Endpoint.Address" in uris[f"{config.product}-{config.environment}-aurora-api-write"]
    assert "ReadEndpoint.Address" in uris[f"{config.product}-{config.environment}-aurora-api-read"]


def test_mysql_engine_runs_wordpress_on_the_writer(config, monkeypatch):
    monkeypatch.setenv("CDK_AURORA_ENGINE", "mysql")
    template = _synth(config)
    template.has_resource_properties("AWS::RDS::DBCluster", {"Engine": "aurora-mysql"})
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {"ContainerDefinitions": [Match.object_like({"Image": Match.string_like_regexp("^wordpress:")})]},
    )
    template.resource_count_is("AWS::ECS::Service", 1)


@pytest.mark.parametrize(("variable", "value"), [("CDK_AURORA_ENGINE", "oracle"), ("CDK_AURORA_INSTANCES", "spot")])
def test_invalid_choices_are_rejected(config, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        _synth(config)


def test_cluster_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::RDS::DBCluster", {"Tags": Match.array_with([tag])})
