"""Unit tests for modules/15_s3. See docs/TESTING.md for how these work."""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

S3Stack = stack_class("15_s3")


def _synth(config):
    app = cdk.App()
    stack = S3Stack(app, "TestS3Stack", config=config)
    return Template.from_stack(stack)


def test_bucket_is_private_encrypted_and_expires_reports(config):
    template = _synth(config)
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
            "LifecycleConfiguration": {
                "Rules": [Match.object_like({"Id": "expire-reports", "Prefix": "reports/", "ExpirationInDays": 30})]
            },
        },
    )


def test_bucket_policy_denies_plain_http(config):
    template = _synth(config)
    policies = template.find_resources("AWS::S3::BucketPolicy")
    assert any('"aws:SecureTransport": "false"' in json.dumps(p) for p in policies.values())


def test_versioning_and_expiration_are_configurable(config, monkeypatch):
    monkeypatch.setenv("CDK_S3_VERSIONED", "true")
    monkeypatch.setenv("CDK_S3_EXPIRE_DAYS", "90")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "VersioningConfiguration": {"Status": "Enabled"},
            "LifecycleConfiguration": {"Rules": [Match.object_like({"ExpirationInDays": 90})]},
        },
    )


def test_writer_may_put_reports_and_reader_may_only_read(config):
    template = _synth(config)
    statements = [
        json.dumps(p["Properties"]["PolicyDocument"]) for p in template.find_resources("AWS::IAM::Policy").values()
    ]
    writers = [s for s in statements if '"s3:PutObject"' in s]
    assert len(writers) == 1 and "reports/*" in writers[0]
    readers = [s for s in statements if '"s3:GetObject*"' in s and '"s3:PutObject"' not in s]
    assert len(readers) == 1


def test_writer_runs_on_a_schedule(config, monkeypatch):
    monkeypatch.setenv("CDK_S3_SCHEDULE", "cron(0 * * * ? *)")
    monkeypatch.setenv("CDK_S3_SCHEDULE_ENABLED", "false")
    template = _synth(config)
    template.resource_count_is("AWS::Scheduler::Schedule", 1)
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "Name": f"{config.product}-{config.environment}-s3-writer",
            "ScheduleExpression": "cron(0 * * * ? *)",
            "State": "DISABLED",
            "Target": Match.object_like({"RetryPolicy": Match.object_like({"MaximumRetryAttempts": 2})}),
        },
    )


def test_two_task_definitions_and_no_service(config):
    template = _synth(config)
    template.resource_count_is("AWS::ECS::TaskDefinition", 2)
    template.resource_count_is("AWS::ECS::Service", 0)
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "Family": f"{config.product}-{config.environment}-s3-writer",
            "ContainerDefinitions": [Match.object_like({"Image": "amazon/aws-cli:2.37.8"})],
        },
    )


def test_bucket_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::S3::Bucket", {"Tags": Match.array_with([tag])})
