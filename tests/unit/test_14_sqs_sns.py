"""Unit tests for modules/14_sqs_sns. See docs/TESTING.md for how these work."""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

SqsSnsStack = stack_class("14_sqs_sns")


def _synth(config):
    app = cdk.App()
    stack = SqsSnsStack(app, "TestSqsSnsStack", config=config)
    return Template.from_stack(stack)


def test_one_topic_two_queues_two_dead_letter_queues(config):
    template = _synth(config)
    template.resource_count_is("AWS::SNS::Topic", 1)
    template.resource_count_is("AWS::SQS::Queue", 4)
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {
            "QueueName": f"{config.product}-{config.environment}-sqs-billing",
            "RedrivePolicy": Match.object_like({"maxReceiveCount": 3}),
            "SqsManagedSseEnabled": True,
        },
    )


def test_shipping_subscription_only_receives_orders(config):
    template = _synth(config)
    subs = template.find_resources("AWS::SNS::Subscription")
    assert len(subs) == 2
    assert all(s["Properties"]["RawMessageDelivery"] is True for s in subs.values())
    policies = [s["Properties"].get("FilterPolicy") for s in subs.values()]
    assert {"kind": ["order"]} in policies and None in policies


def test_producer_may_publish_and_consumers_may_consume(config):
    template = _synth(config)
    statements = [
        json.dumps(p["Properties"]["PolicyDocument"]) for p in template.find_resources("AWS::IAM::Policy").values()
    ]
    assert any('"sns:Publish"' in s for s in statements)
    assert sum('"sqs:ReceiveMessage"' in s and '"sqs:DeleteMessage"' in s for s in statements) == 2


def test_consumers_scale_on_the_queue_backlog(config, monkeypatch):
    monkeypatch.setenv("CDK_SQS_MAX_CONSUMERS", "20")
    template = _synth(config)
    template.resource_count_is("AWS::ApplicationAutoScaling::ScalableTarget", 2)
    template.has_resource_properties(
        "AWS::ApplicationAutoScaling::ScalableTarget", {"MinCapacity": 1, "MaxCapacity": 20}
    )
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"MetricName": "ApproximateNumberOfMessagesVisible", "Namespace": "AWS/SQS"},
    )


def test_three_services_running_the_aws_cli_image(config):
    template = _synth(config)
    template.resource_count_is("AWS::ECS::Service", 3)
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {"ContainerDefinitions": [Match.object_like({"Image": "amazon/aws-cli:2.37.8"})]},
    )


def test_queues_have_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::SQS::Queue", {"Tags": Match.array_with([tag])})
