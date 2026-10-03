"""Unit tests for modules/03_ec2_capacity. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

Ec2CapacityStack = stack_class("03_ec2_capacity")


def _synth(config):
    app = cdk.App()
    stack = Ec2CapacityStack(app, "TestEc2CapacityStack", config=config)
    return Template.from_stack(stack)


def test_launch_template_uses_the_ecs_optimized_al2023_ami_and_imdsv2(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::EC2::LaunchTemplate",
        {
            "LaunchTemplateData": Match.object_like(
                {"InstanceType": "t3.small", "MetadataOptions": Match.object_like({"HttpTokens": "required"})}
            )
        },
    )
    parameters = template.to_json()["Parameters"]
    assert any(
        p.get("Default") == "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
        for p in parameters.values()
    )


def test_instance_role_has_the_ecs_container_instance_policy(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": Match.object_like(
                {"Statement": [Match.object_like({"Principal": {"Service": "ec2.amazonaws.com"}})]}
            ),
            "ManagedPolicyArns": Match.array_with(
                [
                    {
                        "Fn::Join": [
                            "",
                            [
                                "arn:",
                                {"Ref": "AWS::Partition"},
                                ":iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role",
                            ],
                        ]
                    }
                ]
            ),
        },
    )


def test_asg_size_and_capacity_provider_are_configurable(config, monkeypatch):
    monkeypatch.setenv("CDK_EC2_MIN_CAPACITY", "0")
    monkeypatch.setenv("CDK_EC2_MAX_CAPACITY", "10")
    monkeypatch.setenv("CDK_EC2_TARGET_CAPACITY", "80")
    template = _synth(config)
    template.has_resource_properties("AWS::AutoScaling::AutoScalingGroup", {"MinSize": "0", "MaxSize": "10"})
    template.has_resource_properties(
        "AWS::ECS::CapacityProvider",
        {
            "AutoScalingGroupProvider": Match.object_like(
                {
                    "ManagedScaling": Match.object_like({"Status": "ENABLED", "TargetCapacity": 80}),
                    "ManagedTerminationProtection": "DISABLED",
                    "ManagedDraining": "ENABLED",
                }
            )
        },
    )


def test_replica_service_spreads_by_az_then_binpacks_memory(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::Service",
        {
            "ServiceName": f"{config.product}-{config.environment}-ec2-web",
            "PlacementStrategies": [
                {"Type": "spread", "Field": "attribute:ecs.availability-zone"},
                {"Type": "binpack", "Field": "MEMORY"},
            ],
            "CapacityProviderStrategy": [Match.object_like({"Weight": 1})],
        },
    )


def test_node_exporter_runs_as_a_daemon_on_the_host_network(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::Service",
        {"ServiceName": f"{config.product}-{config.environment}-ec2-node-exporter", "SchedulingStrategy": "DAEMON"},
    )
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "NetworkMode": "host",
            "ContainerDefinitions": [Match.object_like({"Image": "prom/node-exporter:v1.9.1"})],
        },
    )


def test_cluster_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ECS::Cluster", {"Tags": Match.array_with([tag])})
