"""Unit tests for modules/20_prometheus_grafana. See docs/TESTING.md for how these work."""

from __future__ import annotations

import importlib
import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

PrometheusGrafanaStack = stack_class("20_prometheus_grafana")
stack_module = importlib.import_module("modules.20_prometheus_grafana.stack")


def _synth(config):
    app = cdk.App()
    stack = PrometheusGrafanaStack(app, "TestPrometheusGrafanaStack", config=config)
    return Template.from_stack(stack)


def _containers(template):
    return [
        c
        for td in template.find_resources("AWS::ECS::TaskDefinition").values()
        for c in td["Properties"]["ContainerDefinitions"]
    ]


def _container(template, image_prefix):
    return next(c for c in _containers(template) if c["Image"].startswith(image_prefix))


def _adot(template):
    adot = _container(template, "amazon/aws-otel-collector")
    (value,) = [e["Value"] for e in adot["Environment"] if e["Name"] == "AOT_CONFIG_CONTENT"]
    return value  # a string, or a CloudFormation intrinsic when it holds tokens


def test_app_task_has_caddy_and_the_adot_sidecar(config):
    template = _synth(config)
    (web,) = [td["Properties"]["ContainerDefinitions"] for td in template.find_resources("AWS::ECS::TaskDefinition")
              .values() if td["Properties"]["Family"].endswith("-prom-web")]
    assert [(c["Name"], c["Image"]) for c in web] == [
        ("app", "caddy:2.11.4-alpine"), ("adot-collector", "amazon/aws-otel-collector:v0.50.0")]
    assert web[1]["Essential"] is True


def test_self_hosted_prometheus_and_grafana_services(config):
    template = _synth(config)
    names = {s["Properties"]["ServiceName"] for s in template.find_resources("AWS::ECS::Service").values()}
    prefix = f"{config.product}-{config.environment}-prom-"
    assert names == {prefix + "web", prefix + "server", prefix + "grafana"}
    server = _container(template, "prom/prometheus:v3.13.4")
    assert "--web.enable-remote-write-receiver" in server["Command"][0]
    template.has_resource_properties(
        "AWS::SecretsManager::Secret", {"Name": f"{config.product}-{config.environment}-secret-grafana"}
    )
    template.resource_count_is("AWS::APS::Workspace", 0)


def test_adot_scrapes_localhost_and_writes_to_the_internal_alb(config):
    adot = json.dumps(_adot(_synth(config)))
    assert "localhost:2019" in adot and "/api/v1/write" in adot and "sigv4auth" not in adot
    assert "awsecscontainermetrics" in adot


def test_monitoring_uis_are_not_open_to_the_internet(config):
    template = _synth(config)
    listeners = template.find_resources("AWS::ElasticLoadBalancingV2::Listener")
    ports = sorted(listener["Properties"]["Port"] for listener in listeners.values())
    assert ports == [80, 3000, 9090, 9090]  # app, Grafana UI, Prometheus UI, internal (remote write)
    ingress = [
        rule for sg in template.find_resources("AWS::EC2::SecurityGroup").values()
        for rule in sg["Properties"].get("SecurityGroupIngress", [])
    ]
    open_ports = sorted(rule["FromPort"] for rule in ingress if rule.get("CidrIp") == "0.0.0.0/0")
    assert open_ports == [80]  # only the app listener


def test_amp_backend(config, monkeypatch):
    monkeypatch.setenv("CDK_PROMETHEUS_BACKEND", "amp")
    template = _synth(config)
    template.resource_count_is("AWS::APS::Workspace", 1)
    template.resource_count_is("AWS::ECS::Service", 1)
    adot = json.dumps(_adot(template))
    assert "sigv4auth" in adot and "api/v1/remote_write" in adot
    template.has_resource_properties(
        "AWS::IAM::Role",
        {"ManagedPolicyArns": Match.array_with([{"Fn::Join": ["", Match.array_with(
            [":iam::aws:policy/AmazonPrometheusRemoteWriteAccess"])]}])},
    )


def test_floci_switches(config, monkeypatch):
    monkeypatch.setenv("CDK_PROMETHEUS_ECS_METRICS", "false")
    monkeypatch.setenv("CDK_PROMETHEUS_SCRAPE_VIA_ALB", "true")
    template = _synth(config)
    adot = json.dumps(_adot(template))
    assert "awsecscontainermetrics" not in adot and "localhost:2019" not in adot
    caddy = _container(template, "caddy")
    (caddyfile,) = [e["Value"] for e in caddy["Environment"] if e["Name"] == "CADDYFILE"]
    assert "handle /metrics" in caddyfile


def test_unknown_backend_is_rejected(config, monkeypatch):
    monkeypatch.setenv("CDK_PROMETHEUS_BACKEND", "influx")
    with pytest.raises(ValueError, match="CDK_PROMETHEUS_BACKEND"):
        _synth(config)


def test_adot_config_shape():
    config = stack_module.adot_config("http://p:9090/api/v1/write", region=None, ecs_metrics=False)
    assert config["service"]["pipelines"]["metrics"]["exporters"] == ["prometheus_remote_write"]
    assert config["processors"]["resource_detection"]["detectors"] == ["ecs", "system"]
    amp = stack_module.adot_config("https://aps/x", region="us-east-1", ecs_metrics=True)
    assert amp["extensions"]["sigv4auth"] == {"region": "us-east-1", "service": "aps"}
    assert "metrics/ecs" in amp["service"]["pipelines"]


def test_prometheus_rules_and_grafana_dashboard():
    rules = {r["alert"] for r in stack_module.RULES["groups"][0]["rules"]}
    assert rules == {"HighErrorRate", "TargetDown"}
    panels = stack_module.DASHBOARD["panels"]
    assert all(p["datasource"]["uid"] == "prometheus" for p in panels)


def test_services_have_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ECS::Service", {"Tags": Match.array_with([tag])})
