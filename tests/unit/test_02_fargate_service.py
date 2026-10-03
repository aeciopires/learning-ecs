"""Unit tests for modules/02_fargate_service. See docs/TESTING.md for how these work."""

from __future__ import annotations

import dataclasses

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

FargateServiceStack = stack_class("02_fargate_service")


def _synth(config):
    app = cdk.App()
    stack = FargateServiceStack(app, "TestFargateServiceStack", config=config)
    return Template.from_stack(stack)


def test_cluster_has_fargate_capacity_providers_and_container_insights(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::ClusterCapacityProviderAssociations",
        {"CapacityProviders": Match.array_with(["FARGATE", "FARGATE_SPOT"])},
    )
    template.has_resource_properties(
        "AWS::ECS::Cluster",
        {"ClusterSettings": [{"Name": "containerInsights", "Value": "enhanced"}]},
    )


def test_task_definition_runs_the_docker_hub_nginx_image_with_a_health_check(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "RequiresCompatibilities": ["FARGATE"],
            "NetworkMode": "awsvpc",
            "Cpu": "256",
            "Memory": "512",
            "RuntimePlatform": {"CpuArchitecture": "X86_64", "OperatingSystemFamily": "LINUX"},
            "ContainerDefinitions": [
                Match.object_like(
                    {
                        "Image": "nginx:1.30-alpine",
                        "HealthCheck": Match.object_like({"Command": Match.array_with(["CMD-SHELL"])}),
                        "LogConfiguration": Match.object_like({"LogDriver": "awslogs"}),
                    }
                )
            ],
        },
    )


def test_container_gets_a_parameter_and_a_secret_as_environment_variables(config):
    template = _synth(config)
    for name in ("APP_GREETING", "APP_API_TOKEN"):
        template.has_resource_properties(
            "AWS::ECS::TaskDefinition",
            {
                "ContainerDefinitions": [
                    Match.object_like({"Secrets": Match.array_with([Match.object_like({"Name": name})])})
                ]
            },
        )


def test_image_cpu_memory_and_architecture_are_configurable(config, monkeypatch):
    monkeypatch.setenv("CDK_FARGATE_IMAGE", "nginx:1.31-alpine")
    monkeypatch.setenv("CDK_FARGATE_CPU", "512")
    monkeypatch.setenv("CDK_FARGATE_MEMORY", "1024")
    monkeypatch.setenv("CDK_CPU_ARCHITECTURE", "arm64")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "Cpu": "512",
            "Memory": "1024",
            "RuntimePlatform": Match.object_like({"CpuArchitecture": "ARM64"}),
            "ContainerDefinitions": [Match.object_like({"Image": "nginx:1.31-alpine"})],
        },
    )


def test_service_is_multi_az_with_circuit_breaker_exec_and_rebalancing(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::Service",
        {
            "DesiredCount": 2,
            "EnableExecuteCommand": True,
            "AvailabilityZoneRebalancing": "ENABLED",
            "PropagateTags": "SERVICE",
            "DeploymentConfiguration": Match.object_like(
                {
                    "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True},
                    "MinimumHealthyPercent": 100,
                    "MaximumPercent": 200,
                }
            ),
            "CapacityProviderStrategy": [{"CapacityProvider": "FARGATE", "Base": 1, "Weight": 1}],
            "NetworkConfiguration": {
                "AwsvpcConfiguration": Match.object_like({"AssignPublicIp": "DISABLED"})
            },
        },
    )


def test_fargate_spot_weight_adds_a_spot_strategy(config, monkeypatch):
    monkeypatch.setenv("CDK_FARGATE_SPOT_WEIGHT", "3")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::Service",
        {
            "CapacityProviderStrategy": [
                {"CapacityProvider": "FARGATE", "Base": 1, "Weight": 1},
                {"CapacityProvider": "FARGATE_SPOT", "Weight": 3},
            ]
        },
    )


def test_no_nat_gateway_puts_tasks_in_public_subnets_with_a_public_ip(config):
    template = _synth(dataclasses.replace(config, nat_gateways=0))
    template.has_resource_properties(
        "AWS::ECS::Service",
        {"NetworkConfiguration": {"AwsvpcConfiguration": Match.object_like({"AssignPublicIp": "ENABLED"})}},
    )


def test_desired_count_module_variable_wins_over_the_global_one(config, monkeypatch):
    monkeypatch.setenv("CDK_DESIRED_COUNT", "4")
    monkeypatch.setenv("CDK_FARGATE_DESIRED_COUNT", "6")
    template = _synth(config)
    template.has_resource_properties("AWS::ECS::Service", {"DesiredCount": 6})


def test_service_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ECS::Service", {"Tags": Match.array_with([tag])})
