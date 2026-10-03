"""Unit tests for modules/05_nlb. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

NlbStack = stack_class("05_nlb")


def _synth(config):
    app = cdk.App()
    stack = NlbStack(app, "TestNlbStack", config=config)
    return Template.from_stack(stack)


def test_one_public_and_one_internal_network_load_balancer_with_security_groups(config):
    template = _synth(config)
    template.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 2)
    for scheme, name in (("internet-facing", "public"), ("internal", "internal")):
        template.has_resource_properties(
            "AWS::ElasticLoadBalancingV2::LoadBalancer",
            {
                "Type": "network",
                "Scheme": scheme,
                "Name": f"{config.product}-{config.environment}-nlb-{name}",
                "SecurityGroups": [Match.any_value()],
                "LoadBalancerAttributes": Match.array_with(
                    [{"Key": "load_balancing.cross_zone.enabled", "Value": "true"}]
                ),
            },
        )


def test_tcp_listeners_on_configurable_ports(config, monkeypatch):
    monkeypatch.setenv("CDK_PORT_NLB_PUBLIC", "8083")
    template = _synth(config)
    template.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {"Port": 8083, "Protocol": "TCP"})
    template.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {"Port": 80, "Protocol": "TCP"})


def test_tcp_target_groups_with_an_http_health_check(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {"Protocol": "TCP", "TargetType": "ip", "HealthCheckProtocol": "HTTP", "HealthCheckPath": "/health"},
    )


def test_public_nlb_is_open_and_internal_nlb_only_to_the_vpc(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {"SecurityGroupIngress": [Match.object_like({"CidrIp": "0.0.0.0/0", "FromPort": 80, "IpProtocol": "tcp"})]},
    )
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {"SecurityGroupIngress": [Match.object_like({"CidrIp": {"Fn::GetAtt": [Match.any_value(), "CidrBlock"]}})]},
    )


def test_tasks_accept_traffic_only_from_the_nlb_security_groups(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"FromPort": 80, "ToPort": 80, "SourceSecurityGroupId": {"Fn::GetAtt": [Match.string_like_regexp("NlbSecurityGroup"), "GroupId"]}},
    )


def test_cross_zone_can_be_turned_off(config, monkeypatch):
    monkeypatch.setenv("CDK_NLB_CROSS_ZONE", "false")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        {"LoadBalancerAttributes": Match.array_with([{"Key": "load_balancing.cross_zone.enabled", "Value": "false"}])},
    )


def test_load_balancers_have_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ElasticLoadBalancingV2::LoadBalancer", {"Tags": Match.array_with([tag])})
