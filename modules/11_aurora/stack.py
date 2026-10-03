"""Module 11 - Aurora: a multi-AZ cluster with a read/write split between two ECS services.

AWS docs used while writing this module:
- aws_rds README, "Starting a clustered database", "Serverless V2 instances in a
  Cluster", "Capacity & Scaling", "Connecting":
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/README.html
- DatabaseCluster / ClusterInstance:
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/DatabaseCluster.html
- Amazon Aurora DB clusters and endpoints (cluster/writer and reader endpoints):
  https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Overview.Endpoints.html
- Aurora Serverless v2: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.html
- High availability for Aurora (failover, promotion tiers):
  https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Concepts.AuroraHighAvailability.html
- ALB listener rule conditions (http-request-method):
  https://docs.aws.amazon.com/elasticloadbalancing/latest/application/rule-condition-types.html

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

from shared.apps import (
    ADMIN_PORT,
    API_PORT,
    CLIENT_COMMAND,
    MIGRATION_SQL,
    MYSQL_CLIENT_COMMAND,
    MYSQL_CLIENT_IMAGE,
    MYSQL_DEFAULT_SQL,
    POSTGRES_CLIENT_IMAGE,
    POSTGREST_IMAGE,
    WORDPRESS_IMAGE,
)
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

STACK_ID = "AuroraStack"

ENGINES = ("postgresql", "mysql")
INSTANCE_KINDS = ("serverless", "provisioned")
DATABASE_NAME = "app"


class AuroraStack(Stack):
    """An Aurora cluster (writer + CDK_AURORA_READERS readers, spread over AZs).

    CDK_AURORA_ENGINE:
      - `postgresql` (default): two PostgREST services - `api-write` on the
        cluster (writer) endpoint and `api-read` on the reader endpoint. The
        ALB sends GET/HEAD to `api-read` and everything else to `api-write`:
        reads scale out with readers, writes go to the single writer.
      - `mysql`: WordPress on the writer endpoint (WordPress doesn't split
        reads and writes).

    CDK_AURORA_INSTANCES: `serverless` (Serverless v2, the default) or
    `provisioned` (CDK_AURORA_INSTANCE_TYPE).
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

        engine_name = env_str("CDK_AURORA_ENGINE", "postgresql").lower()
        if engine_name not in ENGINES:
            raise ValueError(f"CDK_AURORA_ENGINE={engine_name!r}: must be one of {', '.join(ENGINES)}")
        kind = env_str("CDK_AURORA_INSTANCES", "serverless").lower()
        if kind not in INSTANCE_KINDS:
            raise ValueError(f"CDK_AURORA_INSTANCES={kind!r}: must be one of {', '.join(INSTANCE_KINDS)}")
        postgres = engine_name == "postgresql"

        purpose = "aurora"
        self.vpc = build_vpc(self, config, purpose, isolated=True)
        self.cluster = build_cluster(self, config, self.vpc, purpose)

        # --- the Aurora cluster ---------------------------------------------------------
        master_user = "dbadmin"
        self.master_secret, master_secret_name = generated_credentials(self, config, purpose, master_user)
        if postgres:
            version = env_str("CDK_AURORA_POSTGRES_VERSION", "17.9")
            engine = rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.of(version, version.split(".")[0])
            )
        else:
            version = env_str("CDK_AURORA_MYSQL_VERSION", "8.0.mysql_aurora.3.12.0")
            engine = rds.DatabaseClusterEngine.aurora_mysql(version=rds.AuroraMysqlEngineVersion.of(version, "8.0"))

        def instance(instance_id: str, tier: int) -> rds.IClusterInstance:
            if kind == "serverless":
                # scale_with_writer=True puts the reader in promotion tier 1, so
                # it scales with the writer and a failover lands on an
                # instance already sized for the load; otherwise tier 2.
                return rds.ClusterInstance.serverless_v2(instance_id, scale_with_writer=tier <= 1)
            return rds.ClusterInstance.provisioned(
                instance_id, promotion_tier=tier,
                instance_type=ec2.InstanceType(env_str("CDK_AURORA_INSTANCE_TYPE", "t4g.medium")),
            )

        readers = env_int("CDK_AURORA_READERS", 1, minimum=0)
        cluster_name = resource_name(config.product, config.environment, "aurora", engine_name)
        self.database = rds.DatabaseCluster(
            self,
            "Database",
            cluster_identifier=cluster_name,
            engine=engine,
            writer=instance("writer", 0),
            readers=[instance(f"reader{i}", 1 + (i - 1) // 2) for i in range(1, readers + 1)],
            serverless_v2_min_capacity=float(env_str("CDK_AURORA_MIN_ACU", "0.5")),
            serverless_v2_max_capacity=float(env_str("CDK_AURORA_MAX_ACU", "4")),
            credentials=rds.Credentials.from_password(master_user, password_reference(master_secret_name)),
            default_database_name=DATABASE_NAME,
            vpc=self.vpc,
            vpc_subnets=data_subnets(),
            storage_encrypted=True,
            backup=rds.BackupProps(retention=Duration.days(env_int("CDK_AURORA_BACKUP_DAYS", 1, minimum=1))),
            enable_performance_insights=env_bool("CDK_AURORA_PERFORMANCE_INSIGHTS", False),
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
        )
        apply_name_tag(self.database, cluster_name)
        self.database.node.add_dependency(self.master_secret)
        writer_host = self.database.cluster_endpoint.hostname
        reader_host = self.database.cluster_read_endpoint.hostname
        port = cdk.Token.as_string(self.database.cluster_endpoint.port)

        alb_name = bounded_name(config.product, config.environment, "aurora", "alb", max_length=32)
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=alb_name, vpc=self.vpc, internet_facing=True
        )
        apply_name_tag(self.alb, alb_name)
        self.listener = self.alb.add_listener(
            "Http", port=listener_port("AURORA"), protocol=elbv2.ApplicationProtocol.HTTP, open=True
        )
        count = desired_count("CDK_AURORA_DESIRED_COUNT")

        if postgres:
            self.app_secret, _ = generated_credentials(
                self, config, f"{purpose}-app", "authenticator", construct_id="AppSecret"
            )
            self.api_write = self._postgrest(config, "ApiWrite", f"{purpose}-api-write", writer_host, port, count)
            self.api_read = self._postgrest(config, "ApiRead", f"{purpose}-api-read", reader_host, port, count)
            health = elbv2.HealthCheck(path="/ready", port=str(ADMIN_PORT))
            self.listener.add_targets(
                "Write", port=API_PORT, protocol=elbv2.ApplicationProtocol.HTTP, targets=[self.api_write],
                health_check=health, deregistration_delay=Duration.seconds(30),
            )
            # GET/HEAD -> the reader endpoint; everything else (the default) -> the writer.
            self.listener.add_targets(
                "Read", port=API_PORT, protocol=elbv2.ApplicationProtocol.HTTP, targets=[self.api_read],
                health_check=health, deregistration_delay=Duration.seconds(30),
                priority=10, conditions=[elbv2.ListenerCondition.http_request_methods(["GET", "HEAD"])],
            )
            client_image, client_command = POSTGRES_CLIENT_IMAGE, CLIENT_COMMAND
            client_environment = {"PGHOST": writer_host, "PGPORT": port, "PGUSER": master_user,
                                  "PGDATABASE": DATABASE_NAME, "MIGRATION": MIGRATION_SQL, "SQL": ""}
            client_secrets = {
                "PGPASSWORD": ecs.Secret.from_secrets_manager(self.master_secret, "password"),
                "APP_PASSWORD": ecs.Secret.from_secrets_manager(self.app_secret, "password"),
            }
        else:
            self.wordpress = fargate_service(
                self, config, self.cluster, construct_id="WordPress", purpose=f"{purpose}-wordpress",
                image=env_str("CDK_AURORA_WORDPRESS_IMAGE", WORDPRESS_IMAGE), container_port=80, desired=count,
                cpu=512, memory=1024,
                environment={"WORDPRESS_DB_HOST": cdk.Fn.join(":", [writer_host, port]),
                             "WORDPRESS_DB_USER": master_user, "WORDPRESS_DB_NAME": DATABASE_NAME},
                secrets={"WORDPRESS_DB_PASSWORD": ecs.Secret.from_secrets_manager(self.master_secret, "password")},
                health_check_grace_period=Duration.seconds(60),
            )
            self.database.connections.allow_default_port_from(self.wordpress, "WordPress")
            self.listener.add_targets(
                "WordPress", port=80, protocol=elbv2.ApplicationProtocol.HTTP, targets=[self.wordpress],
                health_check=elbv2.HealthCheck(path="/", healthy_http_codes="200-399"),
                stickiness_cookie_duration=Duration.hours(1), deregistration_delay=Duration.seconds(30),
            )
            client_image, client_command = MYSQL_CLIENT_IMAGE, MYSQL_CLIENT_COMMAND
            client_environment = {"DB_HOST": writer_host, "DB_PORT": port, "DB_USER": master_user,
                                  "DB_NAME": DATABASE_NAME, "SQL": MYSQL_DEFAULT_SQL}
            client_secrets = {"DB_PASSWORD": ecs.Secret.from_secrets_manager(self.master_secret, "password")}

        # --- one-off migration / client task ----------------------------------------------
        client_family = resource_name(config.product, config.environment, purpose, "client")
        self.client_task = ecs.FargateTaskDefinition(
            self, "ClientTaskDefinition", family=client_family, cpu=256, memory_limit_mib=512,
            runtime_platform=runtime_platform(),
        )
        apply_name_tag(self.client_task, client_family)
        self.client_task.add_container(
            "client", container_name="client", image=ecs.ContainerImage.from_registry(client_image),
            entry_point=["sh", "-c"], command=[client_command], environment=client_environment,
            secrets=client_secrets, logging=log_driver(self, config, f"{purpose}-client", "ClientLogGroup"),
        )
        self.client_security_group = ec2.SecurityGroup(
            self, "ClientSecurityGroup", vpc=self.vpc, description=f"{client_family} one-off tasks"
        )
        self.database.connections.allow_default_port_from(self.client_security_group, "one-off client task")

        cdk.CfnOutput(self, "Url", value=f"http://{self.alb.load_balancer_dns_name}:{listener_port('AURORA')}/")
        cdk.CfnOutput(self, "WriterEndpoint", value=writer_host)
        cdk.CfnOutput(self, "ReaderEndpoint", value=reader_host)
        cdk.CfnOutput(self, "ClientTaskFamily", value=client_family)
        # The exact revision this deployment registered - safer than "the
        # newest revision of the family" (see README, Verify).
        cdk.CfnOutput(self, "ClientTaskDefinitionArn", value=self.client_task.task_definition_arn)
        cdk.CfnOutput(self, "ClientSecurityGroupId", value=self.client_security_group.security_group_id)
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)

    def _postgrest(self, config, construct_id, purpose, host, port, count) -> ecs.FargateService:
        service = fargate_service(
            self, config, self.cluster, construct_id=construct_id, purpose=purpose,
            image=env_str("CDK_AURORA_API_IMAGE", POSTGREST_IMAGE), container_port=API_PORT, desired=count,
            environment={
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
        service.task_definition.default_container.add_port_mappings(  # type: ignore[union-attr]
            ecs.PortMapping(container_port=ADMIN_PORT, name="admin")
        )
        self.database.connections.allow_default_port_from(service, purpose)
        service.connections.allow_from(self.alb, ec2.Port.tcp(ADMIN_PORT), "ALB health checks (PostgREST /ready)")
        return service


STACK_CLASS = AuroraStack
