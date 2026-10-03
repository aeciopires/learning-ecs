"""Unit tests for modules/04_alb. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

AlbStack = stack_class("04_alb")


def _synth(config, env=None):
    app = cdk.App()
    stack = AlbStack(app, "TestAlbStack", config=config, env=env)
    return Template.from_stack(stack)


def test_one_internet_facing_and_one_internal_alb(config):
    template = _synth(config)
    template.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 2)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {"Name": f"{config.product}-{config.environment}-alb-public", "Scheme": "internet-facing", "Type": "application"},
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {"Name": f"{config.product}-{config.environment}-alb-internal", "Scheme": "internal"},
    )


def test_listeners_default_to_port_80_and_ports_are_configurable(config, monkeypatch):
    template = _synth(config)
    template.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {"Port": 80, "Protocol": "HTTP"})
    monkeypatch.setenv("CDK_PORT_ALB_PUBLIC", "8081")
    monkeypatch.setenv("CDK_PORT_ALB_INTERNAL", "8082")
    template = _synth(config)
    template.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {"Port": 8081})
    template.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {"Port": 8082})


def test_target_groups_use_ip_targets_and_the_health_endpoint(config):
    template = _synth(config)
    template.resource_count_is("AWS::ElasticLoadBalancingV2::TargetGroup", 2)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "TargetType": "ip",
            "HealthCheckPath": "/health",
            "Matcher": {"HttpCode": "200"},
            "TargetGroupAttributes": Match.array_with(
                [{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}]
            ),
        },
    )


def test_admin_paths_get_a_fixed_403(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        {
            "Priority": 10,
            "Actions": [Match.object_like({"Type": "fixed-response", "FixedResponseConfig": Match.object_like({"StatusCode": "403"})})],
        },
    )


def test_internal_alb_only_accepts_traffic_from_the_vpc(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {
            "SecurityGroupIngress": [
                Match.object_like({"CidrIp": {"Fn::GetAtt": [Match.any_value(), "CidrBlock"]}, "FromPort": 80})
            ]
        },
    )


def test_certificate_switches_the_public_listener_to_https_with_a_redirect(config, monkeypatch):
    monkeypatch.setenv(
        "CDK_ALB_CERTIFICATE_ARN", "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000"
    )
    template = _synth(config)
    template.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {"Port": 443, "Protocol": "HTTPS"})
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {"Port": 80, "DefaultActions": [Match.object_like({"Type": "redirect"})]},
    )


def test_access_logs_are_optional(config, monkeypatch):
    template = _synth(config)
    template.resource_count_is("AWS::S3::Bucket", 0)
    monkeypatch.setenv("CDK_ALB_ACCESS_LOGS", "true")
    # Access logging needs a concrete region (the bucket policy names the
    # region's Elastic Load Balancing account), so give the stack one.
    template = _synth(config, env=cdk.Environment(account="123456789012", region="us-east-1"))
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {"LoadBalancerAttributes": Match.array_with([{"Key": "access_logs.s3.enabled", "Value": "true"}])},
    )


def test_two_services_each_registered_with_its_load_balancer(config):
    template = _synth(config)
    template.resource_count_is("AWS::ECS::Service", 2)
    template.has_resource_properties(
        "AWS::ECS::Service",
        {"LoadBalancers": [Match.object_like({"ContainerName": "app", "ContainerPort": 80})], "HealthCheckGracePeriodSeconds": 30},
    )


def test_load_balancers_have_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ElasticLoadBalancingV2::LoadBalancer", {"Tags": Match.array_with([tag])})
