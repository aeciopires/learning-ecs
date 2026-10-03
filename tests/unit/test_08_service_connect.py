"""Unit tests for modules/08_service_connect. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

ServiceConnectStack = stack_class("08_service_connect")


def _synth(config):
    app = cdk.App()
    stack = ServiceConnectStack(app, "TestServiceConnectStack", config=config)
    return Template.from_stack(stack)


def _service(template, name):
    for resource in template.find_resources("AWS::ECS::Service").values():
        if resource["Properties"]["ServiceName"] == name:
            return resource
    raise AssertionError(f"no service {name}")


def test_cluster_has_an_http_namespace_for_service_connect(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ServiceDiscovery::HttpNamespace", {"Name": f"{config.product}-{config.environment}.local"}
    )
    template.resource_count_is("AWS::ServiceDiscovery::PrivateDnsNamespace", 0)


def test_api_is_a_client_server_service_published_as_api_80(config):
    template = _synth(config)
    api = _service(template, f"{config.product}-{config.environment}-sc-api")
    sc = api["Properties"]["ServiceConnectConfiguration"]
    assert sc["Enabled"] is True
    assert sc["Services"][0]["PortName"] == "http"
    assert sc["Services"][0]["ClientAliases"] == [{"DnsName": "api", "Port": 80}]
    assert sc["AccessLogConfiguration"]["Format"] == "JSON"
    assert sc["LogConfiguration"]["LogDriver"] == "awslogs"


def test_api_port_mapping_is_named_and_declares_http(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {"ContainerDefinitions": [Match.object_like({"PortMappings": [Match.object_like({"Name": "http", "AppProtocol": "http"})]})]},
    )


def test_frontend_is_a_client_only_service_pointing_at_the_short_name(config):
    template = _synth(config)
    frontend = _service(template, f"{config.product}-{config.environment}-sc-frontend")
    sc = frontend["Properties"]["ServiceConnectConfiguration"]
    assert sc["Enabled"] is True and "Services" not in sc
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {"ContainerDefinitions": [Match.object_like({"Environment": [{"Name": "UPSTREAM_URL", "Value": "http://api:80"}]})]},
    )


def test_namespace_and_upstream_are_configurable(config, monkeypatch):
    monkeypatch.setenv("CDK_SC_NAMESPACE", "shop.internal")
    monkeypatch.setenv("CDK_SC_UPSTREAM_URL", "http://orders:8080")
    template = _synth(config)
    template.has_resource_properties("AWS::ServiceDiscovery::HttpNamespace", {"Name": "shop.internal"})
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {"ContainerDefinitions": [Match.object_like({"Environment": [{"Name": "UPSTREAM_URL", "Value": "http://orders:8080"}]})]},
    )


def test_tasks_are_sized_for_the_service_connect_proxy(config):
    template = _synth(config)
    for resource in template.find_resources("AWS::ECS::TaskDefinition").values():
        assert resource["Properties"]["Cpu"] == "512"
        assert resource["Properties"]["Memory"] == "1024"


def test_only_the_frontend_is_behind_the_load_balancer(config):
    template = _synth(config)
    template.resource_count_is("AWS::ElasticLoadBalancingV2::TargetGroup", 1)
    frontend = _service(template, f"{config.product}-{config.environment}-sc-frontend")
    api = _service(template, f"{config.product}-{config.environment}-sc-api")
    assert frontend["Properties"]["LoadBalancers"]
    assert "LoadBalancers" not in api["Properties"]


def test_namespace_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ServiceDiscovery::HttpNamespace", {"Tags": Match.array_with([tag])})
