"""Unit tests for modules/18_multi_region. See docs/TESTING.md for how these work."""

from __future__ import annotations

import importlib

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs

stack_module = importlib.import_module("modules.18_multi_region.stack")
ENV = cdk.Environment(account="123456789012", region="eu-west-1")


def _build(config, env=ENV):
    app = cdk.App()
    return {stack.stack_name: stack for stack in stack_module.build_stacks(app, config=config, env=env)}


def test_two_regional_stacks_in_two_regions(config):
    stacks = _build(config)
    assert set(stacks) == {"MultiRegionPrimaryStack", "MultiRegionSecondaryStack"}
    assert stacks["MultiRegionPrimaryStack"].region == "eu-west-1"   # the app's region
    assert stacks["MultiRegionSecondaryStack"].region == "us-west-2"


def test_regions_are_configurable_and_must_differ(config, monkeypatch):
    monkeypatch.setenv("CDK_MULTI_REGION_PRIMARY", "sa-east-1")
    monkeypatch.setenv("CDK_MULTI_REGION_SECONDARY", "us-east-2")
    stacks = _build(config, env=None)
    assert stacks["MultiRegionPrimaryStack"].region == "sa-east-1"
    assert stacks["MultiRegionSecondaryStack"].region == "us-east-2"
    monkeypatch.setenv("CDK_MULTI_REGION_SECONDARY", "sa-east-1")
    with pytest.raises(ValueError, match="both 'sa-east-1'"):
        _build(config, env=None)


def test_each_region_runs_the_service_behind_an_alb(config, monkeypatch):
    monkeypatch.setenv("CDK_PORT_MULTI_REGION_SECONDARY", "8099")
    stacks = _build(config)
    for name, port in (("MultiRegionPrimaryStack", 80), ("MultiRegionSecondaryStack", 8099)):
        template = Template.from_stack(stacks[name])
        template.resource_count_is("AWS::ECS::Service", 1)
        template.has_resource_properties("AWS::ElasticLoadBalancingV2::LoadBalancer", {"Scheme": "internet-facing"})
        template.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {"Port": port})
        for tag in mandatory_tag_pairs(config):
            template.has_resource_properties("AWS::ECS::Service", {"Tags": Match.array_with([tag])})


def test_tasks_report_their_role_and_region(config):
    template = Template.from_stack(_build(config)["MultiRegionSecondaryStack"])
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {"ContainerDefinitions": [Match.object_like(
            {"Environment": [{"Name": "WHOAMI_NAME", "Value": "secondary us-west-2"}]})]},
    )


def test_unknown_role_is_rejected(config):
    with pytest.raises(ValueError, match="role="):
        stack_module.MultiRegionStack(cdk.App(), "X", config=config, role="tertiary")


def test_global_stack_only_once_both_origins_are_known(config, monkeypatch):
    monkeypatch.setenv("CDK_MULTI_REGION_PRIMARY_ORIGIN", "primary.example.com")
    assert "MultiRegionGlobalStack" not in _build(config)
    monkeypatch.setenv("CDK_MULTI_REGION_SECONDARY_ORIGIN", "secondary.example.com")
    stacks = _build(config)
    assert stacks["MultiRegionGlobalStack"].region == "eu-west-1"


def test_distribution_fails_over_between_the_regions(config, monkeypatch):
    monkeypatch.setenv("CDK_MULTI_REGION_PRIMARY_ORIGIN", "primary.example.com")
    monkeypatch.setenv("CDK_MULTI_REGION_SECONDARY_ORIGIN", "secondary.example.com")
    template = Template.from_stack(_build(config)["MultiRegionGlobalStack"])
    (distribution,) = template.find_resources("AWS::CloudFront::Distribution").values()
    dist = distribution["Properties"]["DistributionConfig"]
    domains = [origin["DomainName"] for origin in dist["Origins"]]
    assert domains == ["primary.example.com", "secondary.example.com"]
    assert all(origin["ConnectionAttempts"] == 1 and origin["ConnectionTimeout"] == 3 for origin in dist["Origins"])
    (group,) = dist["OriginGroups"]["Items"]
    assert group["FailoverCriteria"]["StatusCodes"]["Items"] == [500, 502, 503, 504]
    assert dist["DefaultCacheBehavior"]["TargetOriginId"] == group["Id"]
    assert dist["DefaultCacheBehavior"]["AllowedMethods"] == ["GET", "HEAD", "OPTIONS"]
