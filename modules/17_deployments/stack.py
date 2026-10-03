"""Module 17 - Deployments: rolling (circuit breaker + alarms), blue/green, canary and linear.

AWS docs used while writing this module:
- aws_ecs README ("Deployment circuit breaker and rollback", "Deployment alarms",
  "ECS Native Blue/Green Deployment", "Buil-in Linear and Canary Deployments"):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/README.html
- Amazon ECS rolling update deployments / deployment circuit breaker:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-type-ecs.html
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-circuit-breaker.html
- Amazon ECS blue/green deployments and their lifecycle stages:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-type-blue-green.html
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/blue-green-deployment-how-it-works.html
- ALB resources for blue/green, linear and canary deployments:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/alb-resources-for-blue-green.html
- Docker Hub - traefik/whoami: https://hub.docker.com/r/traefik/whoami

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from constructs import Construct

from shared.config import AppConfig, env_bool, env_int, env_str
from shared.ecs import build_cluster, desired_count, fargate_service, listener_port
from shared.naming import bounded_name, resource_name
from shared.network import PUBLIC, build_vpc
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "DeploymentsStack"

DEFAULT_IMAGE = "traefik/whoami:v1.12.0"

STRATEGIES = {
    "rolling": ecs.DeploymentStrategy.ROLLING,
    "blue_green": ecs.DeploymentStrategy.BLUE_GREEN,
    "canary": ecs.DeploymentStrategy.CANARY,
    "linear": ecs.DeploymentStrategy.LINEAR,
}


def deployment_strategy() -> str:
    """CDK_DEPLOYMENT_STRATEGY: rolling (default), blue_green, canary or linear."""
    value = env_str("CDK_DEPLOYMENT_STRATEGY", "rolling").lower()
    if value not in STRATEGIES:
        raise ValueError(f"CDK_DEPLOYMENT_STRATEGY={value!r}: must be one of {', '.join(STRATEGIES)}")
    return value


class DeploymentsStack(Stack):
    """One ALB-fronted service whose deployment strategy is a variable.

    - A production listener (CDK_PORT_DEPLOYMENTS) with one rule and an IP
      target group (blue); for the traffic-shifting strategies also a test
      listener (CDK_PORT_DEPLOYMENTS_TEST) and a second target group (green).
    - rolling: the service lives in the blue target group; the circuit
      breaker and a 5XX alarm roll a bad deployment back.
    - blue_green / canary / linear: ECS moves the listener rules between the
      blue and green target groups itself (a role lets it), with a bake time,
      and rolls back on the same alarm.
    - CDK_DEPLOYMENT_VERSION ends up in every response (`Name: v1`), so
      changing it and deploying again shows a deployment from the outside.
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

        purpose = "deploy"
        strategy = deployment_strategy()
        self.vpc = build_vpc(self, config, purpose)
        self.cluster = build_cluster(self, config, self.vpc, purpose)
        version = env_str("CDK_DEPLOYMENT_VERSION", "v1")

        # --- load balancer, listeners, target groups -------------------------------------
        alb_name = bounded_name(config.product, config.environment, "alb", purpose, max_length=32)
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=alb_name, vpc=self.vpc, internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=PUBLIC),
        )
        apply_name_tag(self.alb, alb_name)
        health_check = elbv2.HealthCheck(
            path="/health", healthy_http_codes="200", interval=Duration.seconds(10),
            healthy_threshold_count=2, unhealthy_threshold_count=2,
        )
        self.blue = self._target_group(config, "BlueTargetGroup", "blue", health_check)
        shifts_traffic = strategy != "rolling"
        # Only the traffic-shifting strategies need the green target group and the test listener.
        self.green = self._target_group(config, "GreenTargetGroup", "green", health_check) if shifts_traffic else None

        prod_port = listener_port("DEPLOYMENTS")
        test_port = listener_port("DEPLOYMENTS_TEST", 8080)  # a port of its own on the same load balancer
        no_route = elbv2.ListenerAction.fixed_response(404, content_type="text/plain", message_body="no route")
        self.prod_listener = self.alb.add_listener(
            "Production", port=prod_port, protocol=elbv2.ApplicationProtocol.HTTP, open=True, default_action=no_route,
        )
        # ECS blue/green moves listener *rules* between the target groups, so traffic goes through one.
        self.prod_rule = elbv2.ApplicationListenerRule(
            self, "ProductionRule", listener=self.prod_listener, priority=1,
            conditions=[elbv2.ListenerCondition.path_patterns(["/*"])],
            action=elbv2.ListenerAction.forward([self.blue]),
        )
        self.test_rule = None
        if self.green is not None:
            # The test listener is for you, not for the internet: only the VPC and CDK_DEPLOYMENT_TEST_CIDR.
            self.test_listener = self.alb.add_listener(
                "Test", port=test_port, protocol=elbv2.ApplicationProtocol.HTTP, open=False, default_action=no_route,
            )
            self.alb.connections.allow_from(
                ec2.Peer.ipv4(env_str("CDK_DEPLOYMENT_TEST_CIDR", self.vpc.vpc_cidr_block)), ec2.Port.tcp(test_port),
                "test listener",
            )
            self.test_rule = elbv2.ApplicationListenerRule(
                self, "TestRule", listener=self.test_listener, priority=1,
                conditions=[elbv2.ListenerCondition.path_patterns(["/*"])],
                action=elbv2.ListenerAction.forward([self.green]),
            )

        # --- the alarm that rolls a deployment back --------------------------------------
        # Its metric comes from the target groups, not the service: no circular dependency.
        alarm_name = resource_name(config.product, config.environment, purpose, "target-5xx")
        groups = {"blue": self.blue} if self.green is None else {"blue": self.blue, "green": self.green}
        self.alarm = cloudwatch.Alarm(
            self, "Target5xxAlarm", alarm_name=alarm_name,
            alarm_description="5XX responses from the service's tasks (all its target groups)",
            metric=cloudwatch.MathExpression(
                expression=" + ".join(f"FILL({key}, 0)" for key in groups),
                using_metrics={
                    key: group.metrics.http_code_target(elbv2.HttpCodeTarget.TARGET_5XX_COUNT, period=Duration.minutes(1))
                    for key, group in groups.items()
                },
                period=Duration.minutes(1),
            ),
            threshold=env_int("CDK_DEPLOYMENT_5XX_THRESHOLD", 5, minimum=1),
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarms = (
            ecs.DeploymentAlarmConfig(alarm_names=[alarm_name], behavior=ecs.AlarmBehavior.ROLLBACK_ON_ALARM)
            if env_bool("CDK_DEPLOYMENT_ALARMS", True)
            else None
        )

        # --- the service -------------------------------------------------------------------------
        strategy_kwargs: dict = {}
        if strategy == "rolling":
            strategy_kwargs["max_healthy_percent"] = env_int("CDK_DEPLOYMENT_MAX_PERCENT", 200, minimum=100)
        else:
            strategy_kwargs["deployment_strategy"] = STRATEGIES[strategy]
            strategy_kwargs["bake_time"] = Duration.minutes(env_int("CDK_DEPLOYMENT_BAKE_MINUTES", 5, minimum=0))
            step = ecs.TrafficShiftConfig(
                step_percent=float(env_str("CDK_DEPLOYMENT_STEP_PERCENT", "10" if strategy == "canary" else "25")),
                step_bake_time=Duration.minutes(env_int("CDK_DEPLOYMENT_STEP_BAKE_MINUTES", 2, minimum=0)),
            )
            if strategy == "canary":
                strategy_kwargs["canary_configuration"] = step
            elif strategy == "linear":
                strategy_kwargs["linear_configuration"] = step

        self.service = fargate_service(
            self, config, self.cluster, construct_id="Web", purpose=f"{purpose}-web",
            image=env_str("CDK_DEPLOYMENT_IMAGE", DEFAULT_IMAGE), container_port=80,
            desired=desired_count("CDK_DEPLOYMENT_DESIRED_COUNT"),
            min_healthy_percent=env_int("CDK_DEPLOYMENT_MIN_HEALTHY_PERCENT", 100, minimum=0),
            environment={"WHOAMI_NAME": version},
            health_check_grace_period=Duration.seconds(30),
            deployment_alarms=alarms,
            # "only supported for services that use the rolling update (ECS) deployment controller"
            circuit_breaker=strategy == "rolling",
            **strategy_kwargs,
        )
        if self.green is None or self.test_rule is None:
            self.blue.add_target(self.service)
        else:
            target = self.service.load_balancer_target(
                container_name="app", container_port=80,
                alternate_target=ecs.AlternateTarget(
                    "AlternateTarget",
                    alternate_target_group=self.green,
                    production_listener=ecs.ListenerRuleConfiguration.application_listener_rule(self.prod_rule),
                    test_listener=ecs.ListenerRuleConfiguration.application_listener_rule(self.test_rule),
                ),
            )
            target.attach_to_application_target_group(self.blue)

        cdk.CfnOutput(self, "Strategy", value=strategy)
        cdk.CfnOutput(self, "ProductionUrl", value=f"http://{self.alb.load_balancer_dns_name}:{prod_port}/")
        if self.green is not None:
            cdk.CfnOutput(self, "TestUrl", value=f"http://{self.alb.load_balancer_dns_name}:{test_port}/")
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        cdk.CfnOutput(self, "ServiceName", value=self.service.service_name)
        cdk.CfnOutput(self, "AlarmName", value=alarm_name)

    def _target_group(self, config, construct_id, color, health_check) -> elbv2.ApplicationTargetGroup:
        name = bounded_name(config.product, config.environment, "tg", "deploy", color, max_length=32)
        group = elbv2.ApplicationTargetGroup(
            self, construct_id, target_group_name=name, vpc=self.vpc, port=80,
            protocol=elbv2.ApplicationProtocol.HTTP, target_type=elbv2.TargetType.IP,
            health_check=health_check, deregistration_delay=Duration.seconds(30),
        )
        apply_name_tag(group, name)
        return group


STACK_CLASS = DeploymentsStack
