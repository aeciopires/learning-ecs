"""Unit tests for modules/21_datadog. See docs/TESTING.md for how these work."""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

DatadogStack = stack_class("21_datadog")


def _synth(config):
    app = cdk.App()
    stack = DatadogStack(app, "TestDatadogStack", config=config)
    return Template.from_stack(stack)


def _web_containers(template):
    (web,) = [td["Properties"]["ContainerDefinitions"] for td in template.find_resources("AWS::ECS::TaskDefinition")
              .values() if td["Properties"]["Family"].endswith("-dd-web")]
    return {c["Name"]: c for c in web}


def test_task_has_app_agent_and_log_router(config):
    containers = _web_containers(_synth(config))
    assert set(containers) == {"app", "datadog-agent", "log_router"}
    assert all(c["Essential"] for c in containers.values())
    assert containers["datadog-agent"]["Image"] == "datadog/agent:7"
    assert containers["log_router"]["Image"] == "amazon/aws-for-fluent-bit:stable"


def test_agent_runs_in_fargate_mode_with_the_key_from_secrets_manager(config):
    agent = _web_containers(_synth(config))["datadog-agent"]
    env = {e["Name"]: e["Value"] for e in agent["Environment"]}
    assert env["ECS_FARGATE"] == "true" and env["DD_SITE"] == "datadoghq.com"
    assert [s["Name"] for s in agent["Secrets"]] == ["DD_API_KEY"]
    assert agent["HealthCheck"]["Command"] == ["CMD-SHELL", "agent health"]
    assert {(p["ContainerPort"], p["Protocol"]) for p in agent["PortMappings"]} == {(8125, "udp"), (8126, "tcp")}


def test_app_logs_go_to_datadog_through_firelens(config):
    containers = _web_containers(_synth(config))
    log = containers["app"]["LogConfiguration"]
    assert log["LogDriver"] == "awsfirelens"
    assert log["Options"]["Name"] == "datadog" and log["Options"]["Host"] == "http-intake.logs.datadoghq.com"
    assert log["Options"]["TLS"] == "on" and log["Options"]["provider"] == "ecs"
    assert [s["Name"] for s in log["SecretOptions"]] == ["apikey"]
    router = containers["log_router"]
    assert router["FirelensConfiguration"] == {"Type": "fluentbit", "Options": {"enable-ecs-log-metadata": "true"}}


def test_unified_service_tagging_and_autodiscovery_on_the_app(config, monkeypatch):
    monkeypatch.setenv("CDK_DATADOG_VERSION", "1.2.3")
    app_container = _web_containers(_synth(config))["app"]
    env = {e["Name"]: e["Value"] for e in app_container["Environment"]}
    service = f"{config.product}-{config.environment}-web"
    assert (env["DD_ENV"], env["DD_SERVICE"], env["DD_VERSION"]) == (config.environment, service, "1.2.3")
    labels = app_container["DockerLabels"]
    assert labels["com.datadoghq.tags.version"] == "1.2.3"
    assert labels["com.datadoghq.tags.service"] == service
    assert json.loads(labels["com.datadoghq.ad.check_names"]) == ["nginx"]
    assert "%%host%%:81/nginx_status/" in labels["com.datadoghq.ad.instances"]
    assert "stub_status" in env["NGINX_CONF"]


def test_site_and_existing_key_are_configurable(config, monkeypatch):
    monkeypatch.setenv("CDK_DATADOG_SITE", "datadoghq.eu")
    monkeypatch.setenv("CDK_DATADOG_API_KEY_SECRET_NAME", "my/datadog/key")
    template = _synth(config)
    template.resource_count_is("AWS::SecretsManager::Secret", 0)
    containers = _web_containers(template)
    assert containers["app"]["LogConfiguration"]["Options"]["Host"] == "http-intake.logs.datadoghq.eu"


def test_placeholder_key_secret_by_default(config):
    _synth(config).has_resource_properties(
        "AWS::SecretsManager::Secret", {"Name": f"{config.product}-{config.environment}-secret-datadog-api-key"}
    )


def test_load_balancer_targets_the_app_container(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::Service", {"LoadBalancers": [Match.object_like({"ContainerName": "app", "ContainerPort": 80})]}
    )


def test_resources_have_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ECS::Service", {"Tags": Match.array_with([tag])})
