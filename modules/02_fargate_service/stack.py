"""Module 02 - Fargate service: cluster, task definition and a service, piece by piece.

AWS docs used while writing this module:
- Cluster: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/Cluster.html
- FargateTaskDefinition: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/FargateTaskDefinition.html
- FargateService: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/FargateService.html
- Amazon ECS task definition parameters (healthCheck, secrets, runtimePlatform):
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html
- Fargate capacity providers: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-capacity-providers.html
- Deployment circuit breaker: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-circuit-breaker.html
- ECS Exec: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-exec.html
- Availability Zone rebalancing: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-rebalancing.html
- Passing secrets/parameters to containers:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html
- Docker Hub - nginx (the image this module runs): https://hub.docker.com/_/nginx

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from shared.config import AppConfig, env_int, env_str
from shared.ecs import (
    build_cluster,
    capacity_provider_strategies,
    desired_count,
    execute_command_enabled,
    log_driver,
    runtime_platform,
)
from shared.naming import resource_name
from shared.network import build_vpc, task_subnets
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "FargateServiceStack"

# Docker Hub's official nginx image - re-check https://hub.docker.com/_/nginx
# for current tags before pinning another one.
DEFAULT_IMAGE = "nginx:1.30-alpine"


class FargateServiceStack(Stack):
    """One Fargate service running nginx (from Docker Hub) in private subnets.

    No load balancer yet (that's module 04) - this module is about what a
    *service* does on its own: keep `desired_count` healthy tasks running
    across Availability Zones, replace the ones that fail their container
    health check, roll back a broken deployment (circuit breaker), and let
    you open a shell in a running container (ECS Exec).
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

        purpose = "fargate"
        self.vpc = build_vpc(self, config, purpose)
        self.cluster = build_cluster(self, config, self.vpc, purpose)

        # --- configuration the container receives -------------------------
        # A plain setting (SSM Parameter Store) and a secret (Secrets
        # Manager): ECS resolves both when the task starts and injects them
        # as environment variables - the values never appear in the task
        # definition. The task *execution* role needs read access to both;
        # `ecs.Secret.from_*` grants it automatically.
        parameter_name = f"/{config.product}/{config.environment}/{purpose}/greeting"
        self.parameter = ssm.StringParameter(
            self,
            "GreetingParameter",
            parameter_name=parameter_name,
            string_value=env_str("CDK_FARGATE_GREETING", "hello from SSM Parameter Store"),
        )
        apply_name_tag(self.parameter, parameter_name)

        secret_name = resource_name(config.product, config.environment, purpose, "api-token")
        self.secret = secretsmanager.Secret(
            self,
            "ApiTokenSecret",
            secret_name=secret_name,
            generate_secret_string=secretsmanager.SecretStringGenerator(exclude_punctuation=True),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        apply_name_tag(self.secret, secret_name)

        # --- task definition: the blueprint ---------------------------------
        family = resource_name(config.product, config.environment, purpose, "web")
        self.task_definition = ecs.FargateTaskDefinition(
            self,
            "TaskDefinition",
            family=family,
            cpu=env_int("CDK_FARGATE_CPU", 256),
            memory_limit_mib=env_int("CDK_FARGATE_MEMORY", 512),
            runtime_platform=runtime_platform(),
        )
        apply_name_tag(self.task_definition, family)
        self.container = self.task_definition.add_container(
            "web",
            container_name="web",
            image=ecs.ContainerImage.from_registry(env_str("CDK_FARGATE_IMAGE", DEFAULT_IMAGE)),
            port_mappings=[ecs.PortMapping(container_port=80, name="http")],
            logging=log_driver(self, config, purpose),
            environment={"APP_ENVIRONMENT": config.environment},
            secrets={
                "APP_GREETING": ecs.Secret.from_ssm_parameter(self.parameter),
                "APP_API_TOKEN": ecs.Secret.from_secrets_manager(self.secret),
            },
            # Docker's HEALTHCHECK, run by ECS inside the container. The
            # alpine nginx image ships busybox wget (no curl).
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "wget -q -O /dev/null http://localhost/ || exit 1"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(10),
            ),
            stop_timeout=Duration.seconds(30),
        )

        # --- service: keeps N copies running ---------------------------------
        service_name = resource_name(config.product, config.environment, purpose, "web")
        self.service = ecs.FargateService(
            self,
            "Service",
            service_name=service_name,
            cluster=self.cluster,
            task_definition=self.task_definition,
            desired_count=desired_count("CDK_FARGATE_DESIRED_COUNT"),
            vpc_subnets=task_subnets(config),
            assign_public_ip=config.tasks_in_public_subnets,
            capacity_provider_strategies=capacity_provider_strategies(),
            # Rolling update: never go below 100% of desired healthy tasks,
            # allow up to 200% while new tasks start.
            min_healthy_percent=100,
            max_healthy_percent=200,
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            enable_execute_command=execute_command_enabled(),
            availability_zone_rebalancing=ecs.AvailabilityZoneRebalancing.ENABLED,
            propagate_tags=ecs.PropagatedTagSource.SERVICE,
            enable_ecs_managed_tags=True,
        )
        apply_name_tag(self.service, service_name)

        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        cdk.CfnOutput(self, "ServiceName", value=self.service.service_name)
        cdk.CfnOutput(self, "TaskDefinitionFamily", value=family)


STACK_CLASS = FargateServiceStack
