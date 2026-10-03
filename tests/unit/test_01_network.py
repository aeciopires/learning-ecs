"""Unit tests for modules/01_network. See docs/TESTING.md for how these work."""

from __future__ import annotations

import dataclasses

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

NetworkStack = stack_class("01_network")


def _synth(config):
    app = cdk.App()
    stack = NetworkStack(app, "TestNetworkStack", config=config)
    return Template.from_stack(stack)


def test_one_vpc_with_the_configured_cidr(config):
    template = _synth(config)
    template.resource_count_is("AWS::EC2::VPC", 1)
    template.has_resource_properties("AWS::EC2::VPC", {"CidrBlock": config.vpc_cidr})


def test_three_subnet_tiers_per_availability_zone(config):
    """public + private + isolated, once per AZ (the fixture uses 2 AZs)."""
    template = _synth(config)
    template.resource_count_is("AWS::EC2::Subnet", 3 * config.max_azs)


def test_nat_gateway_count_follows_the_config(config):
    template = _synth(dataclasses.replace(config, nat_gateways=2))
    template.resource_count_is("AWS::EC2::NatGateway", 2)


def test_no_nat_gateway_means_no_private_tier(config):
    """Without NAT, PRIVATE_WITH_EGRESS subnets can't exist - only public + isolated."""
    template = _synth(dataclasses.replace(config, nat_gateways=0))
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::EC2::Subnet", 2 * config.max_azs)


def test_s3_gateway_endpoint_by_default(config):
    template = _synth(config)
    template.has_resource_properties("AWS::EC2::VPCEndpoint", {"VpcEndpointType": "Gateway"})


def test_s3_gateway_endpoint_can_be_disabled(config, monkeypatch):
    monkeypatch.setenv("CDK_VPC_S3_ENDPOINT", "false")
    template = _synth(config)
    template.resource_count_is("AWS::EC2::VPCEndpoint", 0)


def test_flow_logs_are_off_by_default(config):
    template = _synth(config)
    template.resource_count_is("AWS::EC2::FlowLog", 0)


def test_flow_logs_go_to_cloudwatch_logs_when_enabled(config, monkeypatch):
    monkeypatch.setenv("CDK_VPC_FLOW_LOGS", "true")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::EC2::FlowLog", {"LogDestinationType": "cloud-watch-logs", "TrafficType": "ALL"}
    )
    template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {"LogGroupName": f"/vpc/{config.product}/{config.environment}/flow-logs", "RetentionInDays": 7},
    )


def test_vpc_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::EC2::VPC", {"Tags": Match.array_with([tag])})
    template.has_resource_properties(
        "AWS::EC2::VPC",
        {"Tags": Match.array_with([{"Key": "Name", "Value": f"{config.product}-{config.environment}-vpc-network"}])},
    )
