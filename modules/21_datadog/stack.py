"""Module 21 - Datadog: the Datadog Agent as a sidecar, logs through FireLens, unified service tagging.

Datadog and AWS docs used while writing this module:
- Datadog - Amazon ECS on AWS Fargate: https://docs.datadoghq.com/integrations/ecs_fargate/
- Datadog - Unified service tagging (Amazon ECS):
  https://docs.datadoghq.com/getting_started/tagging/unified_service_tagging/
- Datadog - NGINX integration (Autodiscovery labels, stub_status): https://docs.datadoghq.com/integrations/nginx/
- Amazon ECS - Send logs to an AWS service or AWS Partner (FireLens):
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_firelens.html
- aws_ecs README, "Firelens log driver":
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/README.html
- Docker Hub: datadog/agent, amazon/aws-for-fluent-bit, nginx, curlimages/curl

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from shared.apps import LOAD_COMMAND, LOAD_IMAGE, NGINX_CONF, NGINX_IMAGE, NGINX_START
from shared.config import AppConfig, env_int, env_str
from shared.ecs import (
    build_cluster,
    desired_count,
    fargate_service,
    listener_port,
    log_driver,
    runtime_platform,
)
from shared.naming import bounded_name, resource_name
from shared.network import PUBLIC, build_vpc, task_subnets
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "DatadogStack"

# Docker Hub images - re-check the tags on hub.docker.com.
AGENT_IMAGE = "datadog/agent:7"
LOG_ROUTER_IMAGE = "amazon/aws-for-fluent-bit:stable"
STATUS_PORT = 81

# The app's nginx, plus a stub_status server on :81 for the Datadog NGINX check.
# Port 81 is not mapped to the load balancer; the task's security group only
# lets the load balancer reach port 80.
NGINX_STATUS_SERVER = """server {
  listen 81;
  access_log off;
  location /nginx_status { stub_status; server_tokens on; }
}
"""


class DatadogStack(Stack):
    """An nginx service whose metrics, logs and tags reach Datadog.

    Every task runs three containers:
    - `app` (nginx): JSON access logs, sent by the `awsfirelens` log driver;
      unified service tagging env vars + docker labels; Autodiscovery labels
      for the Datadog NGINX check (stub_status on :81).
    - `datadog-agent` (ECS_FARGATE=true): collects the task's metrics from
      the ECS task metadata endpoint, runs the NGINX check, accepts
      DogStatsD (8125/udp) and traces (8126).
    - `log_router` (aws-for-fluent-bit): the FireLens router that ships the
      app's logs to the Datadog logs intake.
    The API key lives in Secrets Manager; the agent and the log router get
    it as container secrets.
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

        purpose = "dd"
        self.vpc = build_vpc(self, config, purpose)
        self.cluster = build_cluster(self, config, self.vpc, purpose)
        site = env_str("CDK_DATADOG_SITE", "datadoghq.com")
        service_name = env_str("CDK_DATADOG_SERVICE", resource_name(config.product, config.environment, "web"))
        version = env_str("CDK_DATADOG_VERSION", "v1")

        # --- the API key --------------------------------------------------------------------------
        existing = env_str("CDK_DATADOG_API_KEY_SECRET_NAME", "")
        secret_name = existing or resource_name(config.product, config.environment, "secret", "datadog-api-key")
        if existing:
            self.api_key = secretsmanager.Secret.from_secret_name_v2(self, "ApiKey", existing)
        else:
            # A random placeholder: put your real key in it (README), then redeploy the tasks.
            self.api_key = secretsmanager.Secret(
                self, "ApiKey", secret_name=secret_name, description="Datadog API key (replace the placeholder)",
                generate_secret_string=secretsmanager.SecretStringGenerator(exclude_punctuation=True),
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            apply_name_tag(self.api_key, secret_name)
        api_key = ecs.Secret.from_secrets_manager(self.api_key)

        # --- the app: nginx, logs through FireLens, UST and Autodiscovery labels --------------------
        tags = {"env": config.environment, "service": service_name, "version": version}
        self.service = fargate_service(
            self, config, self.cluster, construct_id="Web", purpose=f"{purpose}-web",
            image=env_str("CDK_DATADOG_IMAGE", NGINX_IMAGE), container_port=80,
            desired=desired_count("CDK_DATADOG_DESIRED_COUNT"), cpu=512, memory=1024,
            environment={"NGINX_CONF": NGINX_CONF + NGINX_STATUS_SERVER,
                         # Unified service tagging: on the application container, not the agent.
                         "DD_ENV": tags["env"], "DD_SERVICE": tags["service"], "DD_VERSION": tags["version"]},
            entry_point=["sh", "-c"], command=[NGINX_START],
            container_options={"docker_labels": {
                **{f"com.datadoghq.tags.{key}": value for key, value in tags.items()},
                # Autodiscovery: the agent runs its NGINX check against this container.
                "com.datadoghq.ad.check_names": '["nginx"]',
                "com.datadoghq.ad.init_configs": "[{}]",
                "com.datadoghq.ad.instances": json.dumps(
                    [{"nginx_status_url": f"http://%%host%%:{STATUS_PORT}/nginx_status/"}]),
            }},
            logging=ecs.LogDrivers.firelens(
                options={
                    "Name": "datadog", "Host": f"http-intake.logs.{site}", "TLS": "on", "provider": "ecs",
                    "dd_service": tags["service"], "dd_source": "nginx", "dd_message_key": "log",
                    "dd_tags": f"env:{tags['env']},version:{tags['version']},team:{config.team_owner}",
                },
                secret_options={"apikey": api_key},
            ),
            health_check_grace_period=Duration.seconds(30),
        )
        task = self.service.task_definition
        task.add_firelens_log_router(
            "LogRouter", container_name="log_router",
            image=ecs.ContainerImage.from_registry(env_str("CDK_DATADOG_LOG_ROUTER_IMAGE", LOG_ROUTER_IMAGE)),
            essential=True, memory_reservation_mib=64,
            firelens_config=ecs.FirelensConfig(
                type=ecs.FirelensLogRouterType.FLUENTBIT,
                options=ecs.FirelensOptions(enable_ecs_log_metadata=True),
            ),
            # The router's own output goes to CloudWatch, so delivery problems are visible.
            logging=log_driver(self, config, f"{purpose}-web-log-router", "LogRouterLogGroup"),
        )
        task.add_container(
            "DatadogAgent", container_name="datadog-agent",
            image=ecs.ContainerImage.from_registry(env_str("CDK_DATADOG_AGENT_IMAGE", AGENT_IMAGE)),
            essential=True, memory_reservation_mib=256,
            environment={"ECS_FARGATE": "true", "DD_SITE": site, "DD_APM_ENABLED": "true"},
            secrets={"DD_API_KEY": api_key},
            port_mappings=[ecs.PortMapping(container_port=8125, protocol=ecs.Protocol.UDP),
                           ecs.PortMapping(container_port=8126, protocol=ecs.Protocol.TCP)],
            health_check=ecs.HealthCheck(command=["CMD-SHELL", "agent health"], interval=Duration.seconds(30)),
            logging=log_driver(self, config, f"{purpose}-web-agent", "AgentLogGroup"),
        )

        # --- load balancer -----------------------------------------------------------------------------
        alb_name = bounded_name(config.product, config.environment, "alb", purpose, max_length=32)
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=alb_name, vpc=self.vpc, internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=PUBLIC),
        )
        apply_name_tag(self.alb, alb_name)
        port = listener_port("DATADOG")
        listener = self.alb.add_listener("Http", port=port, protocol=elbv2.ApplicationProtocol.HTTP, open=True)
        listener.add_targets(
            "Web", target_group_name=bounded_name(config.product, config.environment, "tg", purpose, max_length=32),
            port=80, protocol=elbv2.ApplicationProtocol.HTTP,
            # The app container, explicitly: the task has more than one container with port mappings.
            targets=[self.service.load_balancer_target(container_name="app", container_port=80)],
            health_check=elbv2.HealthCheck(path="/health", healthy_http_codes="200", interval=Duration.seconds(15)),
            deregistration_delay=Duration.seconds(30),
        )

        # --- load generator ------------------------------------------------------------------------------
        family = resource_name(config.product, config.environment, f"{purpose}-load")
        self.load_task = ecs.FargateTaskDefinition(
            self, "LoadTaskDefinition", family=family, cpu=256, memory_limit_mib=512, runtime_platform=runtime_platform(),
        )
        apply_name_tag(self.load_task, family)
        self.load_task.add_container(
            "app", container_name="app", image=ecs.ContainerImage.from_registry(LOAD_IMAGE),
            entry_point=["sh", "-c"], command=[LOAD_COMMAND],
            environment={
                "URL": f"http://{self.alb.load_balancer_dns_name}:{port}/",
                "WORKERS": str(env_int("CDK_DATADOG_LOAD_WORKERS", 5, minimum=1)),
                "DURATION": str(env_int("CDK_DATADOG_LOAD_SECONDS", 300, minimum=1)),
                "ERROR_EVERY": str(env_int("CDK_DATADOG_LOAD_ERROR_EVERY", 20, minimum=1)),
            },
            logging=log_driver(self, config, f"{purpose}-load", "LoadLogGroup"),
        )
        self.load_security_group = ec2.SecurityGroup(
            self, "LoadSecurityGroup", vpc=self.vpc, description="load generator (egress only)"
        )

        cdk.CfnOutput(self, "Url", value=f"http://{self.alb.load_balancer_dns_name}:{port}/")
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        cdk.CfnOutput(self, "ServiceName", value=self.service.service_name)
        cdk.CfnOutput(self, "DatadogService", value=service_name)
        cdk.CfnOutput(self, "ApiKeySecretName", value=secret_name)
        cdk.CfnOutput(self, "LoadTaskDefinitionArn", value=self.load_task.task_definition_arn)
        cdk.CfnOutput(
            self, "TaskSubnetIds",
            value=cdk.Fn.join(",", self.vpc.select_subnets(subnet_group_name=task_subnets(config).subnet_group_name).subnet_ids),
        )
        cdk.CfnOutput(self, "LoadSecurityGroupId", value=self.load_security_group.security_group_id)


STACK_CLASS = DatadogStack
