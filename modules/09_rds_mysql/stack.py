"""Module 09 - RDS for MySQL: WordPress on Fargate reading and writing a MySQL database.

AWS docs used while writing this module:
- DatabaseInstance: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/DatabaseInstance.html
- MysqlEngineVersion: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/MysqlEngineVersion.html
- Multi-AZ DB instance deployments: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZSingleStandby.html
- Amazon RDS for MySQL: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_MySQL.html
- Passing Secrets Manager secrets to ECS containers:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html
- Docker Hub - wordpress (WORDPRESS_DB_* variables): https://hub.docker.com/_/wordpress
- Docker Hub - mysql (the client in the one-off task): https://hub.docker.com/_/mysql

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

from shared.apps import MYSQL_CLIENT_COMMAND, WORDPRESS_IMAGE
from shared.apps import MYSQL_CLIENT_IMAGE as CLIENT_IMAGE
from shared.apps import MYSQL_DEFAULT_SQL as DEFAULT_SQL
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

STACK_ID = "RdsMysqlStack"

DATABASE_NAME = "wordpress"


class RdsMysqlStack(Stack):
    """public ALB -> WordPress (Fargate) -> RDS for MySQL (isolated subnets).

    - The database lives in the isolated subnets (no route to the internet)
      and only accepts connections on 3306 from the two task security
      groups that need it (WordPress and the client task).
    - `CDK_RDS_MULTI_AZ=true` adds a synchronous standby in a second AZ;
      RDS fails over to it automatically.
    - WordPress gets the host, user and database name as plain environment
      variables, and the password as an ECS `secrets` entry read from
      Secrets Manager at task start.
    - A second task definition (`*-rds-mysql-client`, the official mysql
      image) is not a service: you run it on demand with `aws ecs run-task`
      to write and read a table - the "migration / one-off job" pattern.
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

        purpose = "rds-mysql"
        self.vpc = build_vpc(self, config, purpose, isolated=True)
        self.cluster = build_cluster(self, config, self.vpc, purpose)

        # --- the database -------------------------------------------------------
        username = "wpadmin"
        self.secret, secret_name = generated_credentials(self, config, purpose, username)
        version = env_str("CDK_RDS_MYSQL_VERSION", "8.4.10")
        instance_name = resource_name(config.product, config.environment, "rds", "mysql")
        self.database = rds.DatabaseInstance(
            self,
            "Database",
            instance_identifier=instance_name,
            engine=rds.DatabaseInstanceEngine.mysql(
                version=rds.MysqlEngineVersion.of(version, ".".join(version.split(".")[:2]))
            ),
            instance_type=ec2.InstanceType(env_str("CDK_RDS_INSTANCE_TYPE", "t3.micro")),
            vpc=self.vpc,
            vpc_subnets=data_subnets(),
            multi_az=env_bool("CDK_RDS_MULTI_AZ", False),
            credentials=rds.Credentials.from_password(username, password_reference(secret_name)),
            database_name=DATABASE_NAME,
            allocated_storage=env_int("CDK_RDS_STORAGE_GB", 20, minimum=20),
            max_allocated_storage=env_int("CDK_RDS_MAX_STORAGE_GB", 100, minimum=20),
            storage_encrypted=True,
            backup_retention=Duration.days(env_int("CDK_RDS_BACKUP_DAYS", 1, minimum=0)),
            enable_performance_insights=env_bool("CDK_RDS_PERFORMANCE_INSIGHTS", False),
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
        )
        apply_name_tag(self.database, instance_name)
        self.database.node.add_dependency(self.secret)
        host = self.database.db_instance_endpoint_address
        port = self.database.db_instance_endpoint_port

        # --- WordPress, behind a public ALB ---------------------------------------
        self.wordpress = fargate_service(
            self, config, self.cluster, construct_id="WordPress", purpose=f"{purpose}-wordpress",
            image=env_str("CDK_RDS_MYSQL_WORDPRESS_IMAGE", WORDPRESS_IMAGE), container_port=80,
            desired=desired_count("CDK_RDS_MYSQL_DESIRED_COUNT"), cpu=512, memory=1024,
            environment={
                "WORDPRESS_DB_HOST": cdk.Fn.join(":", [host, port]),
                "WORDPRESS_DB_USER": username,
                "WORDPRESS_DB_NAME": DATABASE_NAME,
            },
            secrets={"WORDPRESS_DB_PASSWORD": ecs.Secret.from_secrets_manager(self.secret, "password")},
            health_check_grace_period=Duration.seconds(60),
        )
        self.database.connections.allow_default_port_from(self.wordpress, "WordPress")

        alb_name = bounded_name(config.product, config.environment, "rds-mysql", "alb", max_length=32)
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=alb_name, vpc=self.vpc, internet_facing=True
        )
        apply_name_tag(self.alb, alb_name)
        self.alb.add_listener(
            "Http", port=listener_port("RDS_MYSQL"), protocol=elbv2.ApplicationProtocol.HTTP, open=True
        ).add_targets(
            "WordPress", port=80, targets=[self.wordpress], deregistration_delay=Duration.seconds(30),
            # Before the install wizard runs, "/" answers 302 -> /wp-admin/install.php.
            health_check=elbv2.HealthCheck(path="/", healthy_http_codes="200-399"),
            # WordPress sessions are cookie-based; keep a browser on one task.
            stickiness_cookie_duration=Duration.hours(1),
        )

        # --- a one-off client task (not a service) -----------------------------------
        client_family = resource_name(config.product, config.environment, purpose, "client")
        self.client_task = ecs.FargateTaskDefinition(
            self, "ClientTaskDefinition", family=client_family, cpu=256, memory_limit_mib=512,
            runtime_platform=runtime_platform(),
        )
        apply_name_tag(self.client_task, client_family)
        self.client_task.add_container(
            "client",
            container_name="client",
            image=ecs.ContainerImage.from_registry(env_str("CDK_RDS_MYSQL_CLIENT_IMAGE", CLIENT_IMAGE)),
            entry_point=["sh", "-c"],
            command=[MYSQL_CLIENT_COMMAND],
            environment={"DB_HOST": host, "DB_PORT": port, "DB_USER": username, "DB_NAME": DATABASE_NAME,
                         "SQL": DEFAULT_SQL},
            secrets={"DB_PASSWORD": ecs.Secret.from_secrets_manager(self.secret, "password")},
            logging=log_driver(self, config, f"{purpose}-client", "ClientLogGroup"),
        )
        self.client_security_group = ec2.SecurityGroup(
            self, "ClientSecurityGroup", vpc=self.vpc, description=f"{client_family} one-off tasks"
        )
        self.database.connections.allow_default_port_from(self.client_security_group, "one-off client task")

        cdk.CfnOutput(self, "Url", value=f"http://{self.alb.load_balancer_dns_name}:{listener_port('RDS_MYSQL')}/")
        cdk.CfnOutput(self, "DbEndpoint", value=cdk.Fn.join(":", [host, port]))
        cdk.CfnOutput(self, "SecretName", value=secret_name)
        cdk.CfnOutput(self, "ClientTaskFamily", value=client_family)
        # The exact revision this deployment registered - safer than "the
        # newest revision of the family" (see README, Verify).
        cdk.CfnOutput(self, "ClientTaskDefinitionArn", value=self.client_task.task_definition_arn)
        cdk.CfnOutput(self, "ClientSecurityGroupId", value=self.client_security_group.security_group_id)
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)


STACK_CLASS = RdsMysqlStack
