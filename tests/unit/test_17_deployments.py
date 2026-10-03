"""Unit tests for modules/17_deployments. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

DeploymentsStack = stack_class("17_deployments")


def _synth(config):
    app = cdk.App()
    stack = DeploymentsStack(app, "TestDeploymentsStack", config=config)
    return Template.from_stack(stack)


def _service(template):
    (service,) = template.find_resources("AWS::ECS::Service").values()
    return service["Properties"]


def test_rolling_is_the_default_with_circuit_breaker_and_alarm_rollback(config):
    template = _synth(config)
    props = _service(template)
    deployment = props["DeploymentConfiguration"]
    assert "Strategy" not in deployment
    assert deployment["DeploymentCircuitBreaker"] == {"Enable": True, "Rollback": True}
    assert deployment["Alarms"] == {
        "AlarmNames": [f"{config.product}-{config.environment}-deploy-target-5xx"], "Enable": True, "Rollback": True,
    }
    assert deployment["MaximumPercent"] == 200 and deployment["MinimumHealthyPercent"] == 100
    assert "AdvancedConfiguration" not in props["LoadBalancers"][0]
    # rolling needs neither the green target group nor the test listener
    template.resource_count_is("AWS::ElasticLoadBalancingV2::TargetGroup", 1)
    template.resource_count_is("AWS::ElasticLoadBalancingV2::Listener", 1)


def test_the_rollback_alarm_watches_target_5xx(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "AlarmName": f"{config.product}-{config.environment}-deploy-target-5xx",
            "Threshold": 5,
            "EvaluationPeriods": 2,
            "TreatMissingData": "notBreaching",
            "Metrics": Match.array_with(
                [Match.object_like({"MetricStat": Match.object_like({"Metric": Match.object_like(
                    {"MetricName": "HTTPCode_Target_5XX_Count", "Namespace": "AWS/ApplicationELB"})})})]
            ),
        },
    )


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        ("blue_green", {"Strategy": "BLUE_GREEN", "BakeTimeInMinutes": 5}),
        ("canary", {"Strategy": "CANARY", "CanaryConfiguration": {"CanaryPercent": 10, "CanaryBakeTimeInMinutes": 2}}),
        ("linear", {"Strategy": "LINEAR", "LinearConfiguration": {"StepPercent": 25, "StepBakeTimeInMinutes": 2}}),
    ],
)
def test_traffic_shifting_strategies(config, monkeypatch, strategy, expected):
    monkeypatch.setenv("CDK_DEPLOYMENT_STRATEGY", strategy)
    template = _synth(config)
    props = _service(template)
    assert expected.items() <= props["DeploymentConfiguration"].items()
    assert "DeploymentCircuitBreaker" not in props["DeploymentConfiguration"]
    advanced = props["LoadBalancers"][0]["AdvancedConfiguration"]
    assert {"AlternateTargetGroupArn", "ProductionListenerRule", "TestListenerRule", "RoleArn"} <= advanced.keys()
    template.resource_count_is("AWS::ElasticLoadBalancingV2::TargetGroup", 2)
    ports = sorted(listener["Properties"]["Port"]
                   for listener in template.find_resources("AWS::ElasticLoadBalancingV2::Listener").values())
    assert ports == [80, 8080]  # production and test listeners can't share a port
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": Match.object_like({"Statement": [Match.object_like(
                {"Principal": {"Service": "ecs.amazonaws.com"}})]}),
            "ManagedPolicyArns": [Match.object_like({"Fn::Join": ["", Match.array_with(
                [":iam::aws:policy/AmazonECSInfrastructureRolePolicyForLoadBalancers"])]})],
        },
    )


def test_test_listener_only_accepts_the_vpc_or_a_given_cidr(config, monkeypatch):
    monkeypatch.setenv("CDK_DEPLOYMENT_STRATEGY", "blue_green")
    monkeypatch.setenv("CDK_DEPLOYMENT_TEST_CIDR", "203.0.113.10/32")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {"SecurityGroupIngress": Match.array_with([Match.object_like({"CidrIp": "203.0.113.10/32", "FromPort": 8080})])},
    )


def test_unknown_strategy_is_rejected(config, monkeypatch):
    monkeypatch.setenv("CDK_DEPLOYMENT_STRATEGY", "big_bang")
    with pytest.raises(ValueError, match="CDK_DEPLOYMENT_STRATEGY"):
        _synth(config)


def test_version_is_visible_in_responses_and_alarms_can_be_disabled(config, monkeypatch):
    monkeypatch.setenv("CDK_DEPLOYMENT_VERSION", "v2")
    monkeypatch.setenv("CDK_DEPLOYMENT_ALARMS", "false")
    template = _synth(config)
    assert _service(template)["DeploymentConfiguration"]["Alarms"]["Enable"] is False
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {"ContainerDefinitions": [Match.object_like({"Environment": [{"Name": "WHOAMI_NAME", "Value": "v2"}]})]},
    )


def test_service_and_target_groups_have_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ECS::Service", {"Tags": Match.array_with([tag])})
        template.has_resource_properties("AWS::ElasticLoadBalancingV2::TargetGroup", {"Tags": Match.array_with([tag])})
