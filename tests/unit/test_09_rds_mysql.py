"""Unit tests for modules/09_rds_mysql. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

RdsMysqlStack = stack_class("09_rds_mysql")


def _synth(config):
    app = cdk.App()
    stack = RdsMysqlStack(app, "TestRdsMysqlStack", config=config)
    return Template.from_stack(stack)


def test_mysql_instance_in_isolated_subnets_with_encryption_and_backups(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::RDS::DBInstance",
        {
            "Engine": "mysql",
            "EngineVersion": "8.4.10",
            "DBInstanceClass": "db.t3.micro",
            "DBName": "wordpress",
            "MultiAZ": False,
            "StorageEncrypted": True,
            "BackupRetentionPeriod": 1,
            "AllocatedStorage": "20",
            "MaxAllocatedStorage": 100,
        },
    )


def test_password_is_a_by_name_secrets_manager_reference(config):
    template = _synth(config)
    instance = next(iter(template.find_resources("AWS::RDS::DBInstance").values()))
    password = instance["Properties"]["MasterUserPassword"]
    assert password == (
        f"{{{{resolve:secretsmanager:{config.product}-{config.environment}-secret-rds-mysql:SecretString:password::}}}}"
    )


def test_engine_version_size_and_multi_az_are_configurable(config, monkeypatch):
    monkeypatch.setenv("CDK_RDS_MYSQL_VERSION", "8.0.42")
    monkeypatch.setenv("CDK_RDS_INSTANCE_TYPE", "m7g.large")
    monkeypatch.setenv("CDK_RDS_MULTI_AZ", "true")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::RDS::DBInstance", {"EngineVersion": "8.0.42", "DBInstanceClass": "db.m7g.large", "MultiAZ": True}
    )


def test_wordpress_gets_the_database_settings_and_the_password_as_a_secret(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "ContainerDefinitions": [
                Match.object_like(
                    {
                        "Image": "wordpress:6.9-apache",
                        "Environment": Match.array_with(
                            [Match.object_like({"Name": "WORDPRESS_DB_NAME", "Value": "wordpress"})]
                        ),
                        "Secrets": [Match.object_like({"Name": "WORDPRESS_DB_PASSWORD"})],
                    }
                )
            ]
        },
    )


def test_database_only_accepts_wordpress_and_the_client_task(config):
    template = _synth(config)
    rules = [
        r for r in template.find_resources("AWS::EC2::SecurityGroupIngress").values()
        if r["Properties"].get("FromPort") == 3306 or "Port" in str(r["Properties"].get("FromPort"))
    ]
    assert len(rules) == 2


def test_one_off_client_task_definition_runs_the_mysql_client(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ECS::TaskDefinition",
        {
            "Family": f"{config.product}-{config.environment}-rds-mysql-client",
            "ContainerDefinitions": [
                Match.object_like({"Image": "mysql:8.4", "Secrets": [Match.object_like({"Name": "DB_PASSWORD"})]})
            ],
        },
    )
    # A task definition only - not a service.
    template.resource_count_is("AWS::ECS::Service", 1)


def test_alb_health_check_accepts_the_installer_redirect(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup", {"Matcher": {"HttpCode": "200-399"}, "HealthCheckPath": "/"}
    )


def test_database_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::RDS::DBInstance", {"Tags": Match.array_with([tag])})
