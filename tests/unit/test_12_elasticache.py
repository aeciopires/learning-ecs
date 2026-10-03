"""Unit tests for modules/12_elasticache. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

ElastiCacheStack = stack_class("12_elasticache")


def _synth(config):
    app = cdk.App()
    stack = ElastiCacheStack(app, "TestElastiCacheStack", config=config)
    return Template.from_stack(stack)


def _environment(template, family_suffix):
    for resource in template.find_resources("AWS::ECS::TaskDefinition").values():
        if resource["Properties"]["Family"].endswith(family_suffix):
            return {e["Name"]: e["Value"] for e in resource["Properties"]["ContainerDefinitions"][0]["Environment"]}
    raise AssertionError(family_suffix)


def test_multi_az_valkey_with_automatic_failover_and_encryption(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ElastiCache::ReplicationGroup",
        {
            "Engine": "valkey",
            "EngineVersion": "8.1",
            "CacheNodeType": "cache.t4g.micro",
            "NumCacheClusters": 2,
            "AutomaticFailoverEnabled": True,
            "MultiAZEnabled": True,
            "TransitEncryptionEnabled": True,
            "AtRestEncryptionEnabled": True,
        },
    )


def test_no_replicas_means_no_failover(config, monkeypatch):
    monkeypatch.setenv("CDK_CACHE_REPLICAS", "0")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ElastiCache::ReplicationGroup",
        {"NumCacheClusters": 1, "AutomaticFailoverEnabled": False, "MultiAZEnabled": False},
    )


def test_tasks_write_to_the_primary_and_read_from_the_reader_endpoint_over_tls(config):
    env = _environment(_synth(config), "elasticache-counter")
    assert "PrimaryEndPoint.Address" in str(env["CACHE_HOST"])
    assert "ReaderEndPoint.Address" in str(env["READER_HOST"])
    assert env["CACHE_TLS"].startswith("--tls --cacert ")


def test_only_the_two_task_security_groups_reach_the_cache(config):
    template = _synth(config)
    groups = template.find_resources("AWS::EC2::SecurityGroup")
    cache_group = next(r for logical_id, r in groups.items() if logical_id.startswith("CacheSecurityGroup"))
    rules = cache_group["Properties"]["SecurityGroupIngress"]
    assert len(rules) == 2
    assert all(rule["FromPort"] == 6379 and "SourceSecurityGroupId" in rule for rule in rules)


def test_existing_endpoint_skips_the_replication_group(config, monkeypatch):
    monkeypatch.setenv("CDK_CACHE_ENDPOINT", "floci")
    monkeypatch.setenv("CDK_CACHE_TLS", "false")
    template = _synth(config)
    template.resource_count_is("AWS::ElastiCache::ReplicationGroup", 0)
    env = _environment(template, "elasticache-client")
    assert env["CACHE_HOST"] == "floci" and env["CACHE_TLS"] == ""


def test_redis_engine(config, monkeypatch):
    monkeypatch.setenv("CDK_CACHE_ENGINE", "redis")
    template = _synth(config)
    template.has_resource_properties("AWS::ElastiCache::ReplicationGroup", {"Engine": "redis", "EngineVersion": "7.1"})


def test_invalid_engine_is_rejected(config, monkeypatch):
    monkeypatch.setenv("CDK_CACHE_ENGINE", "memcached")
    with pytest.raises(ValueError, match="CDK_CACHE_ENGINE"):
        _synth(config)


def test_replication_group_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::ElastiCache::ReplicationGroup", {"Tags": Match.array_with([tag])})
