"""Unit tests for modules/16_autoscaling. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

AutoscalingStack = stack_class("16_autoscaling")


def _synth(config):
    app = cdk.App()
    stack = AutoscalingStack(app, "TestAutoscalingStack", config=config)
    return Template.from_stack(stack)


def _policies(template):
    return {
        p["Properties"]["TargetTrackingScalingPolicyConfiguration"]["PredefinedMetricSpecification"][
            "PredefinedMetricType"
        ]: p["Properties"]["TargetTrackingScalingPolicyConfiguration"]
        for p in template.find_resources("AWS::ApplicationAutoScaling::ScalingPolicy").values()
    }


def test_service_is_a_scalable_target_with_scheduled_actions(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ApplicationAutoScaling::ScalableTarget",
        {
            "MinCapacity": 2,
            "MaxCapacity": 10,
            "ScalableDimension": "ecs:service:DesiredCount",
            "ScheduledActions": [
                Match.object_like({"Schedule": "cron(0 8 ? * MON-FRI *)", "Timezone": "UTC",
                                   "ScalableTargetAction": {"MinCapacity": 4}}),
                Match.object_like({"Schedule": "cron(0 20 ? * MON-FRI *)", "ScalableTargetAction": {"MinCapacity": 2}}),
            ],
        },
    )


def test_three_target_tracking_policies(config):
    policies = _policies(_synth(config))
    assert set(policies) == {"ECSServiceAverageCPUUtilization", "ECSServiceAverageMemoryUtilization",
                             "ALBRequestCountPerTarget"}
    assert policies["ECSServiceAverageCPUUtilization"]["TargetValue"] == 60
    assert policies["ECSServiceAverageMemoryUtilization"]["TargetValue"] == 75
    assert policies["ALBRequestCountPerTarget"]["TargetValue"] == 1000
    assert policies["ECSServiceAverageCPUUtilization"]["ScaleInCooldown"] == 300
    assert policies["ECSServiceAverageCPUUtilization"]["ScaleOutCooldown"] == 60


def test_a_zero_target_turns_a_policy_off_and_schedule_can_be_disabled(config, monkeypatch):
    monkeypatch.setenv("CDK_AUTOSCALING_MEMORY_TARGET", "0")
    monkeypatch.setenv("CDK_AUTOSCALING_REQUESTS_PER_TARGET", "0")
    monkeypatch.setenv("CDK_AUTOSCALING_SCHEDULE_ENABLED", "false")
    template = _synth(config)
    assert set(_policies(template)) == {"ECSServiceAverageCPUUtilization"}
    targets = template.find_resources("AWS::ApplicationAutoScaling::ScalableTarget")
    assert all("ScheduledActions" not in t["Properties"] for t in targets.values())


def test_capacity_and_schedule_are_configurable(config, monkeypatch):
    monkeypatch.setenv("CDK_AUTOSCALING_MIN", "1")
    monkeypatch.setenv("CDK_AUTOSCALING_MAX", "20")
    monkeypatch.setenv("CDK_AUTOSCALING_PEAK_MIN", "8")
    monkeypatch.setenv("CDK_AUTOSCALING_TIMEZONE", "America/Sao_Paulo")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ApplicationAutoScaling::ScalableTarget",
        {
            "MinCapacity": 1,
            "MaxCapacity": 20,
            "ScheduledActions": Match.array_with(
                [Match.object_like({"Timezone": "America/Sao_Paulo", "ScalableTargetAction": {"MinCapacity": 8}})]
            ),
        },
    )
    template.has_resource_properties("AWS::ECS::Service", {"DesiredCount": 1})


@pytest.mark.parametrize(
    ("variables", "message"),
    [
        ({"CDK_AUTOSCALING_MIN": "5", "CDK_AUTOSCALING_MAX": "3"}, "CDK_AUTOSCALING_MAX"),
        ({"CDK_AUTOSCALING_PEAK_MIN": "50"}, "CDK_AUTOSCALING_PEAK_MIN"),
    ],
)
def test_inconsistent_capacity_is_rejected(config, monkeypatch, variables, message):
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        _synth(config)


def test_load_generator_targets_the_alb(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "Family": f"{config.product}-{config.environment}-scale-load",
            "ContainerDefinitions": [
                Match.object_like(
                    {"Image": "curlimages/curl:8.22.0",
                     "Environment": Match.array_with([{"Name": "WORKERS", "Value": "20"}])}
                )
            ],
        },
    )


def test_scalable_resources_have_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ECS::Service", {"Tags": Match.array_with([tag])})
        template.has_resource_properties("AWS::ElasticLoadBalancingV2::LoadBalancer", {"Tags": Match.array_with([tag])})
