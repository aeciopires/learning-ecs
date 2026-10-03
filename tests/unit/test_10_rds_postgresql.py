"""Unit tests for modules/10_rds_postgresql. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

RdsPostgresqlStack = stack_class("10_rds_postgresql")


def _synth(config):
    app = cdk.App()
    stack = RdsPostgresqlStack(app, "TestRdsPostgresqlStack", config=config)
    return Template.from_stack(stack)


def _container(template, image_prefix):
    for resource in template.find_resources("AWS::ECS::TaskDefinition").values():
        container = resource["Properties"]["ContainerDefinitions"][0]
        if container["Image"].startswith(image_prefix):
            return container
    raise AssertionError(image_prefix)


def test_postgres_instance_and_two_generated_secrets(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::RDS::DBInstance",
        {"Engine": "postgres", "EngineVersion": "17.9", "DBName": "app", "StorageEncrypted": True},
    )
    template.resource_count_is("AWS::SecretsManager::Secret", 2)


def test_version_is_configurable(config, monkeypatch):
    monkeypatch.setenv("CDK_RDS_POSTGRES_VERSION", "16.9")
    template = _synth(config)
    template.has_resource_properties("AWS::RDS::DBInstance", {"EngineVersion": "16.9"})


def test_postgrest_connects_as_authenticator_with_the_app_secret(config):
    container = _container(_synth(config), "postgrest/postgrest")
    env = {e["Name"]: e["Value"] for e in container["Environment"]}
    assert env["PGRST_DB_SCHEMAS"] == "api"
    assert env["PGRST_DB_ANON_ROLE"] == "web_anon"
    assert env["PGRST_ADMIN_SERVER_PORT"] == "3001"
    assert "postgres://authenticator@" in str(env["PGRST_DB_URI"])
    assert [s["Name"] for s in container["Secrets"]] == ["PGPASSWORD"]
    assert {p["ContainerPort"] for p in container["PortMappings"]} == {3000, 3001}


def test_alb_health_checks_ready_on_the_admin_port(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {"Port": 3000, "HealthCheckPath": "/ready", "HealthCheckPort": "3001"},
    )
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress", {"FromPort": 3001, "ToPort": 3001, "Description": Match.string_like_regexp("PostgREST")}
    )


def test_client_task_runs_the_migration_with_both_secrets(config):
    container = _container(_synth(config), "postgres:")
    env = {e["Name"]: e["Value"] for e in container["Environment"]}
    assert "CREATE TABLE IF NOT EXISTS api.todos" in env["MIGRATION"]
    assert "NOTIFY pgrst, 'reload schema'" in env["MIGRATION"]
    assert env["PGUSER"] == "pgadmin"
    assert {s["Name"] for s in container["Secrets"]} == {"PGPASSWORD", "APP_PASSWORD"}


def test_database_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::RDS::DBInstance", {"Tags": Match.array_with([tag])})
