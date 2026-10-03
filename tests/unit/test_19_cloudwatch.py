"""Unit tests for modules/19_cloudwatch. See docs/TESTING.md for how these work."""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

CloudWatchStack = stack_class("19_cloudwatch")


def _synth(config):
    app = cdk.App()
    stack = CloudWatchStack(app, "TestCloudWatchStack", config=config)
    return Template.from_stack(stack)


def test_cluster_has_enhanced_container_insights(config):
    _synth(config).has_resource_properties(
        "AWS::ECS::Cluster", {"ClusterSettings": [{"Name": "containerInsights", "Value": "enhanced"}]}
    )


def test_container_has_health_check_restart_policy_and_json_logs(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "Family": f"{config.product}-{config.environment}-obs-web",
            "ContainerDefinitions": [Match.object_like({
                "Image": "nginx:1.30-alpine",
                "HealthCheck": Match.object_like({"Command": ["CMD-SHELL", Match.string_like_regexp("wget")]}),
                "RestartPolicy": Match.object_like({"Enabled": True, "RestartAttemptPeriod": 60}),
                "Environment": [Match.object_like({"Name": "NGINX_CONF",
                                                   "Value": Match.string_like_regexp("log_format json_log")})],
            })],
        },
    )


def test_metric_filters_turn_logs_into_metrics(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::Logs::MetricFilter",
        {"FilterPattern": "{ $.status >= 500 }", "MetricTransformations": [Match.object_like(
            {"MetricName": "Http5xxCount", "MetricNamespace": f"{config.product}/{config.environment}",
             "MetricValue": "1", "DefaultValue": 0})]},
    )
    template.has_resource_properties(
        "AWS::Logs::MetricFilter",
        {"MetricTransformations": [Match.object_like({"MetricName": "RequestTime", "MetricValue": "$.request_time"})]},
    )


def test_four_alarms_notify_one_topic(config):
    template = _synth(config)
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    names = {a["Properties"]["AlarmName"] for a in alarms.values()}
    prefix = f"{config.product}-{config.environment}-obs-"
    assert names == {prefix + n for n in ("app-5xx", "unhealthy-targets", "high-cpu", "tasks-below-desired")}
    for alarm in alarms.values():
        props = alarm["Properties"]
        assert props["AlarmActions"] == props["OKActions"] and len(props["AlarmActions"]) == 1
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"AlarmName": prefix + "tasks-below-desired", "Namespace": "ECS/ContainerInsights",
         "MetricName": "RunningTaskCount", "ComparisonOperator": "LessThanThreshold", "Threshold": 2,
         "TreatMissingData": "breaching"},
    )
    template.resource_count_is("AWS::SNS::Subscription", 0)


def test_optional_email_subscription(config, monkeypatch):
    monkeypatch.setenv("CDK_CLOUDWATCH_ALARM_EMAIL", "oncall@example.com")
    _synth(config).has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "oncall@example.com"}
    )


def test_stopped_tasks_are_kept_in_a_log_group(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::Events::Rule",
        {"EventPattern": Match.object_like({"source": ["aws.ecs"], "detail-type": ["ECS Task State Change"],
                                            "detail": Match.object_like({"lastStatus": ["STOPPED"]})})},
    )
    template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {"LogGroupName": f"/aws/events/{config.product}/{config.environment}/obs-stopped-tasks"},
    )


def test_dashboard_and_saved_queries(config):
    template = _synth(config)
    (dashboard,) = template.find_resources("AWS::CloudWatch::Dashboard").values()
    body = json.dumps(dashboard["Properties"]["DashboardBody"])
    for expected in ("RunningTaskCount", "TargetResponseTime", "Http5xxCount", "alarms", "log"):
        assert expected in body
    names = {q["Properties"]["Name"] for q in template.find_resources("AWS::Logs::QueryDefinition").values()}
    assert names == {f"{config.product}/{config.environment}/{n}"
                     for n in ("errors-by-path", "slowest-requests", "requests-per-minute", "stopped-tasks")}


def test_resources_have_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ECS::Service", {"Tags": Match.array_with([tag])})
        template.has_resource_properties("AWS::CloudWatch::Alarm", {"Tags": Match.array_with([tag])})
