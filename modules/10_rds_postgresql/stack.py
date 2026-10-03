"""Module 10 - RDS for PostgreSQL: a REST API (PostgREST) on Fargate over a PostgreSQL database.

AWS docs used while writing this module:
- DatabaseInstance / PostgresEngineVersion:
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/DatabaseInstance.html
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/PostgresEngineVersion.html
- Amazon RDS for PostgreSQL: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_PostgreSQL.html
- Health checks on a different port (target group health check port):
  https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-health-checks.html
- PostgREST configuration (PGRST_DB_URI, PGRST_DB_SCHEMAS, PGRST_DB_ANON_ROLE,
  PGRST_ADMIN_SERVER_PORT, /live and /ready) and tutorial 0 (schema api, web_anon,
  authenticator): https://docs.postgrest.org/en/stable/references/configuration.html
  https://docs.postgrest.org/en/stable/tutorials/tut0.html
- Docker Hub - postgrest/postgrest and postgres: https://hub.docker.com/r/postgrest/postgrest
  https://hub.docker.com/_/postgres

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_rds as rds
from constructs import Construct

from shared.apps import ADMIN_PORT, API_PORT, CLIENT_COMMAND, MIGRATION_SQL, POSTGREST_IMAGE
from shared.apps import POSTGRES_CLIENT_IMAGE as CLIENT_IMAGE
from shared.config import AppConfig, env_bool, env_int, env_str
from shared.database import generated_credentials, password_reference
from shared.ecs import (
    build_cluster,
    desired_count,
    fargate_service,
    listener_port,
    log_driver,
    runtime_platform,
)
from shared.naming import bounded_name, resource_name
from shared.network import build_vpc, data_subnets
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "RdsPostgresqlStack"

DATABASE_NAME = "app"


class RdsPostgresqlStack(Stack):
    """public ALB -> PostgREST (Fargate) -> RDS for PostgreSQL (isolated subnets).

    - Two generated secrets: the master user (used only by the one-off
      migration/client task) and `authenticator`, the low-privilege login
      PostgREST uses - the app never holds the master password.
    - PostgREST serves the `api` schema on port 3000 and its health
      endpoints (/live, /ready) on an admin port, 3001; the ALB health
      checks /ready on 3001 while traffic goes to 3000.
    - The one-off task (official postgres image) runs the migration by
      default, or any SQL you pass in the `SQL` variable with --overrides.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        apply_standard_tags(self, tags=config.to_standard_tags())

        purpose = "rds-postgres"
        self.vpc = build_vpc(self, config, purpose, isolated=True)
        self.cluster = build_cluster(self, config, self.vpc, purpose)

        # --- the database -------------------------------------------------------------
        master_user = "pgadmin"
        self.master_secret, master_secret_name = generated_credentials(self, config, purpose, master_user)
        self.app_secret, _ = generated_credentials(
            self, config, f"{purpose}-app", "authenticator", construct_id="AppSecret"
        )
        version = env_str("CDK_RDS_POSTGRES_VERSION", "17.9")
        instance_name = resource_name(config.product, config.environment, "rds", "postgres")
        self.database = rds.DatabaseInstance(
            self,
            "Database",
            instance_identifier=instance_name,
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.of(version, version.split(".")[0])
            ),
            instance_type=ec2.InstanceType(env_str("CDK_RDS_INSTANCE_TYPE", "t3.micro")),
            vpc=self.vpc,
            vpc_subnets=data_subnets(),
            multi_az=env_bool("CDK_RDS_MULTI_AZ", False),
            credentials=rds.Credentials.from_password(master_user, password_reference(master_secret_name)),
            database_name=DATABASE_NAME,
            allocated_storage=env_int("CDK_RDS_STORAGE_GB", 20, minimum=20),
            max_allocated_storage=env_int("CDK_RDS_MAX_STORAGE_GB", 100, minimum=20),
            storage_encrypted=True,
            backup_retention=Duration.days(env_int("CDK_RDS_BACKUP_DAYS", 1, minimum=0)),
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
        )
        apply_name_tag(self.database, instance_name)
        self.database.node.add_dependency(self.master_secret)
        host = self.database.db_instance_endpoint_address
        port = self.database.db_instance_endpoint_port

        # --- PostgREST --------------------------------------------------------------------
        self.api = fargate_service(
            self, config, self.cluster, construct_id="Api", purpose=f"{purpose}-api",
            image=env_str("CDK_RDS_POSTGRES_API_IMAGE", POSTGREST_IMAGE), container_port=API_PORT,
            desired=desired_count("CDK_RDS_POSTGRES_DESIRED_COUNT"),
            environment={
                # The password comes from PGPASSWORD (a libpq variable PostgREST honours).
                "PGRST_DB_URI": cdk.Fn.join("", ["postgres://authenticator@", host, ":", port, f"/{DATABASE_NAME}"]),
                "PGRST_DB_SCHEMAS": "api",
                "PGRST_DB_ANON_ROLE": "web_anon",
                "PGRST_SERVER_PORT": str(API_PORT),
                "PGRST_ADMIN_SERVER_PORT": str(ADMIN_PORT),
                # error (the default) logs only 5xx; info logs every request.
                "PGRST_LOG_LEVEL": env_str("CDK_POSTGREST_LOG_LEVEL", "info"),
            },
            secrets={"PGPASSWORD": ecs.Secret.from_secrets_manager(self.app_secret, "password")},
            health_check_grace_period=Duration.seconds(60),
        )
        self.api.task_definition.default_container.add_port_mappings(  # type: ignore[union-attr]
            ecs.PortMapping(container_port=ADMIN_PORT, name="admin")
        )
        self.database.connections.allow_default_port_from(self.api, "PostgREST")

        alb_name = bounded_name(config.product, config.environment, "rds-pg", "alb", max_length=32)
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=alb_name, vpc=self.vpc, internet_facing=True
        )
        apply_name_tag(self.alb, alb_name)
        self.alb.add_listener(
            "Http", port=listener_port("RDS_POSTGRESQL"), protocol=elbv2.ApplicationProtocol.HTTP, open=True
        ).add_targets(
            "Api", port=API_PORT, protocol=elbv2.ApplicationProtocol.HTTP, targets=[self.api],
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(path="/ready", port=str(ADMIN_PORT)),
        )
        # The health checks reach the tasks on the admin port too.
        self.api.connections.allow_from(self.alb, ec2.Port.tcp(ADMIN_PORT), "ALB health checks (PostgREST /ready)")

        # --- one-off migration / client task ----------------------------------------------
        client_family = resource_name(config.product, config.environment, purpose, "client")
        self.client_task = ecs.FargateTaskDefinition(
            self, "ClientTaskDefinition", family=client_family, cpu=256, memory_limit_mib=512,
            runtime_platform=runtime_platform(),
        )
        apply_name_tag(self.client_task, client_family)
        self.client_task.add_container(
            "client",
            container_name="client",
            image=ecs.ContainerImage.from_registry(env_str("CDK_RDS_POSTGRES_CLIENT_IMAGE", CLIENT_IMAGE)),
            entry_point=["sh", "-c"],
            command=[CLIENT_COMMAND],
            # libpq variables psql reads: PGHOST, PGPORT, PGUSER, PGDATABASE, PGPASSWORD.
            environment={"PGHOST": host, "PGPORT": port, "PGUSER": master_user, "PGDATABASE": DATABASE_NAME,
                         "MIGRATION": MIGRATION_SQL, "SQL": ""},
            secrets={
                "PGPASSWORD": ecs.Secret.from_secrets_manager(self.master_secret, "password"),
                "APP_PASSWORD": ecs.Secret.from_secrets_manager(self.app_secret, "password"),
            },
            logging=log_driver(self, config, f"{purpose}-client", "ClientLogGroup"),
        )
        self.client_security_group = ec2.SecurityGroup(
            self, "ClientSecurityGroup", vpc=self.vpc, description=f"{client_family} one-off tasks"
        )
        self.database.connections.allow_default_port_from(self.client_security_group, "one-off client task")

        cdk.CfnOutput(self, "Url", value=f"http://{self.alb.load_balancer_dns_name}:{listener_port('RDS_POSTGRESQL')}/")
        cdk.CfnOutput(self, "DbEndpoint", value=cdk.Fn.join(":", [host, port]))
        cdk.CfnOutput(self, "ClientTaskFamily", value=client_family)
        # The exact revision this deployment registered - safer than "the
        # newest revision of the family" (see README, Verify).
        cdk.CfnOutput(self, "ClientTaskDefinitionArn", value=self.client_task.task_definition_arn)
        cdk.CfnOutput(self, "ClientSecurityGroupId", value=self.client_security_group.security_group_id)
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)


STACK_CLASS = RdsPostgresqlStack
