"""Unit tests for modules/13_documentdb. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

DocumentDbStack = stack_class("13_documentdb")


def _synth(config):
    app = cdk.App()
    stack = DocumentDbStack(app, "TestDocumentDbStack", config=config)
    return Template.from_stack(stack)


def _containers(template, family_suffix):
    for resource in template.find_resources("AWS::ECS::TaskDefinition").values():
        if resource["Properties"]["Family"].endswith(family_suffix):
            return {c["Name"]: c for c in resource["Properties"]["ContainerDefinitions"]}, resource["Properties"]
    raise AssertionError(family_suffix)


def test_two_instance_encrypted_cluster_with_a_by_name_password(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::DocDB::DBCluster", {"EngineVersion": "5.0.0", "StorageEncrypted": True, "MasterUsername": "docdbadmin"}
    )
    template.resource_count_is("AWS::DocDB::DBInstance", 2)
    cluster = next(iter(template.find_resources("AWS::DocDB::DBCluster").values()))
    assert "resolve:secretsmanager" in cluster["Properties"]["MasterUserPassword"]


def test_tls_tasks_fetch_the_ca_bundle_before_mongosh_starts(config):
    containers, task = _containers(_synth(config), "documentdb-client")
    fetch, mongosh = containers["ca-bundle"], containers["mongosh"]
    assert fetch["Essential"] is False and fetch["User"] == "0"
    assert "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem" in fetch["Command"]
    assert mongosh["DependsOn"] == [{"Condition": "SUCCESS", "ContainerName": "ca-bundle"}]
    assert mongosh["MountPoints"] == [{"ContainerPath": "/certs", "ReadOnly": True, "SourceVolume": "certs"}]
    assert task["Volumes"] == [{"Name": "certs"}]
    env = {e["Name"]: e["Value"] for e in mongosh["Environment"]}
    assert env["DOCDB_TLS"] == "--tls --tlsCAFile /certs/global-bundle.pem"


def test_without_tls_there_is_no_init_container_and_a_tls_disabled_parameter_group(config, monkeypatch):
    monkeypatch.setenv("CDK_DOCDB_TLS", "false")
    template = _synth(config)
    containers, task = _containers(template, "documentdb-events")
    assert list(containers) == ["mongosh"] and "Volumes" not in task
    template.has_resource_properties(
        "AWS::DocDB::DBClusterParameterGroup", {"Family": "docdb5.0", "Parameters": {"tls": "disabled"}}
    )


def test_password_reaches_the_tasks_as_a_secret(config):
    containers, _ = _containers(_synth(config), "documentdb-events")
    assert [s["Name"] for s in containers["mongosh"]["Secrets"]] == ["DOCDB_PASSWORD"]


def test_existing_cluster_mode_creates_no_cluster(config, monkeypatch):
    monkeypatch.setenv("CDK_DOCDB_ENDPOINT", "172.26.0.4")
    template = _synth(config)
    template.resource_count_is("AWS::DocDB::DBCluster", 0)
    template.resource_count_is("AWS::SecretsManager::Secret", 0)
    containers, _ = _containers(template, "documentdb-client")
    env = {e["Name"]: e["Value"] for e in containers["mongosh"]["Environment"]}
    assert env["DOCDB_HOST"] == "172.26.0.4"


def test_only_the_task_security_groups_reach_the_cluster(config):
    template = _synth(config)
    groups = template.find_resources("AWS::EC2::SecurityGroup")
    db_group = next(r for logical_id, r in groups.items() if logical_id.startswith("DocDbSecurityGroup"))
    rules = db_group["Properties"]["SecurityGroupIngress"]
    assert len(rules) == 2 and all(r["FromPort"] == 27017 for r in rules)


def test_cluster_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::DocDB::DBCluster", {"Tags": Match.array_with([tag])})
