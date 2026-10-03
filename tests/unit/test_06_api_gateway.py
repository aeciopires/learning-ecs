"""Unit tests for modules/06_api_gateway. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

ApiGatewayStack = stack_class("06_api_gateway")


def _synth(config):
    app = cdk.App()
    stack = ApiGatewayStack(app, "TestApiGatewayStack", config=config)
    return Template.from_stack(stack)


def test_default_is_a_rest_api_with_a_vpc_link_to_an_internal_nlb(config):
    template = _synth(config)
    template.resource_count_is("AWS::ApiGateway::RestApi", 1)
    template.resource_count_is("AWS::ApiGateway::VpcLink", 1)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Type": "network", "Scheme": "internal"}
    )
    template.has_resource_properties(
        "AWS::ApiGateway::Method",
        {
            "HttpMethod": "ANY",
            "RequestParameters": {"method.request.path.proxy": True},
            "Integration": Match.object_like(
                {
                    "Type": "HTTP_PROXY",
                    "ConnectionType": "VPC_LINK",
                    "RequestParameters": {"integration.request.path.proxy": "method.request.path.proxy"},
                }
            ),
        },
    )


def test_stage_has_throttling_metrics_and_access_logs(config, monkeypatch):
    monkeypatch.setenv("CDK_APIGW_RATE_LIMIT", "10")
    monkeypatch.setenv("CDK_APIGW_BURST_LIMIT", "20")
    monkeypatch.setenv("CDK_APIGW_DETAILED_METRICS", "true")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ApiGateway::Stage",
        {
            "StageName": config.environment,
            "MethodSettings": [
                Match.object_like({"ThrottlingRateLimit": 10, "ThrottlingBurstLimit": 20, "MetricsEnabled": True})
            ],
            "AccessLogSetting": Match.object_like({"DestinationArn": Match.any_value()}),
        },
    )


def test_access_logs_can_be_disabled(config, monkeypatch):
    monkeypatch.setenv("CDK_APIGW_ACCESS_LOGS", "false")
    template = _synth(config)
    template.resource_count_is("AWS::Logs::LogGroup", 1)  # only the ECS service's own log group
    template.resource_count_is("AWS::ApiGateway::Account", 0)


def test_internet_connection_uses_a_public_nlb_and_no_vpc_link(config, monkeypatch):
    monkeypatch.setenv("CDK_APIGW_CONNECTION", "internet")
    template = _synth(config)
    template.resource_count_is("AWS::ApiGateway::VpcLink", 0)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Type": "network", "Scheme": "internet-facing"}
    )
    template.has_resource_properties(
        "AWS::ApiGateway::Method", {"Integration": Match.object_like({"ConnectionType": "INTERNET"})}
    )


def test_backend_url_override(config, monkeypatch):
    monkeypatch.setenv("CDK_APIGW_CONNECTION", "internet")
    monkeypatch.setenv("CDK_APIGW_BACKEND_URL", "http://localhost:8085/")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ApiGateway::Method", {"Integration": Match.object_like({"Uri": "http://localhost:8085/{proxy}"})}
    )


def test_http_api_with_a_vpc_link_to_an_internal_alb(config, monkeypatch):
    monkeypatch.setenv("CDK_APIGW_TYPE", "http")
    template = _synth(config)
    template.resource_count_is("AWS::ApiGatewayV2::Api", 1)
    template.resource_count_is("AWS::ApiGatewayV2::VpcLink", 1)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Type": "application", "Scheme": "internal"}
    )
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Integration", {"IntegrationType": "HTTP_PROXY", "ConnectionType": "VPC_LINK"}
    )
    template.has_resource_properties("AWS::ApiGatewayV2::Route", {"RouteKey": "ANY /{proxy+}"})


@pytest.mark.parametrize(("variable", "value"), [("CDK_APIGW_TYPE", "graphql"), ("CDK_APIGW_CONNECTION", "peering")])
def test_invalid_choices_are_rejected(config, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        _synth(config)


def test_rest_api_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ApiGateway::RestApi", {"Tags": Match.array_with([tag])})
