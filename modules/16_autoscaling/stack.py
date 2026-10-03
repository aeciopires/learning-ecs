"""Module 16 - Service Auto Scaling: target tracking (CPU, memory, ALB requests) + scheduled scaling.

AWS docs used while writing this module:
- aws_ecs README, "Task Auto-Scaling":
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/README.html
- aws_applicationautoscaling README ("Scheduled scaling"):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_applicationautoscaling/README.html
- Automatically scale your Amazon ECS service:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html
- Use a target metric to scale Amazon ECS services:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-autoscaling-targettracking.html
- How target tracking scaling works (cooldowns, multiple policies):
  https://docs.aws.amazon.com/autoscaling/application/userguide/target-tracking-scaling-policy-overview.html
- How scheduled scaling works:
  https://docs.aws.amazon.com/autoscaling/application/userguide/scheduled-scaling-policy-overview.html
- Docker Hub - traefik/whoami: https://hub.docker.com/r/traefik/whoami
- Docker Hub - curlimages/curl: https://hub.docker.com/r/curlimages/curl

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_applicationautoscaling as appscaling
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from constructs import Construct

from shared.config import AppConfig, env_bool, env_int, env_str
from shared.ecs import build_cluster, fargate_service, listener_port, log_driver, runtime_platform
from shared.naming import bounded_name, resource_name
from shared.network import PUBLIC, build_vpc, task_subnets
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "AutoscalingStack"

# Docker Hub images - re-check the tags on hub.docker.com.
DEFAULT_IMAGE = "traefik/whoami:v1.12.0"
LOAD_IMAGE = "curlimages/curl:8.22.0"

# WORKERS parallel loops of `curl $URL` for DURATION seconds, then a summary.
LOAD_COMMAND = (
    'end=$(( $(date +%s) + DURATION )); echo "load: $WORKERS workers for $DURATION seconds against $URL"; '
    'for i in $(seq 1 "$WORKERS"); do '
    '( n=0; while [ "$(date +%s)" -lt "$end" ]; do curl -s -o /dev/null "$URL" && n=$((n+1)); done; '
    'echo "worker $i: $n requests" ) & done; wait; echo "load: done"'
)


class AutoscalingStack(Stack):
    """One ALB-fronted service that scales itself, and a task that loads it.

    - `auto_scale_task_count` registers the service as a scalable target
      (min..max tasks).
    - Three target tracking policies - average CPU, average memory and ALB
      requests per target. Scale-out happens if ANY policy asks for it,
      scale-in only when ALL of them agree.
    - Two scheduled actions raise the minimum on weekday mornings and lower
      it again in the evening (pre-scaling for a known peak).
    - A standalone load-generator task (curl loops) produces the traffic
      that makes the request-count policy scale out.
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

        purpose = "scale"
        self.vpc = build_vpc(self, config, purpose)
        self.cluster = build_cluster(self, config, self.vpc, purpose)

        min_tasks = env_int("CDK_AUTOSCALING_MIN", 2, minimum=0)
        max_tasks = env_int("CDK_AUTOSCALING_MAX", 10, minimum=1)
        if max_tasks < min_tasks:
            raise ValueError(f"CDK_AUTOSCALING_MAX={max_tasks} is lower than CDK_AUTOSCALING_MIN={min_tasks}")

        self.service = fargate_service(
            self, config, self.cluster, construct_id="Web", purpose=f"{purpose}-web",
            image=env_str("CDK_AUTOSCALING_IMAGE", DEFAULT_IMAGE), container_port=80, desired=min_tasks,
            environment={"WHOAMI_NAME": "scale-web"}, health_check_grace_period=Duration.seconds(30),
        )

        # --- the load balancer ---------------------------------------------------------
        port = listener_port("AUTOSCALING")
        alb_name = bounded_name(config.product, config.environment, "alb", purpose, max_length=32)
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=alb_name, vpc=self.vpc, internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=PUBLIC),
        )
        apply_name_tag(self.alb, alb_name)
        listener = self.alb.add_listener("Http", port=port, protocol=elbv2.ApplicationProtocol.HTTP, open=True)
        self.target_group = listener.add_targets(
            "Web",
            target_group_name=bounded_name(config.product, config.environment, "tg", purpose, max_length=32),
            port=80, protocol=elbv2.ApplicationProtocol.HTTP, targets=[self.service],
            health_check=elbv2.HealthCheck(path="/health", healthy_http_codes="200", interval=Duration.seconds(15)),
            deregistration_delay=Duration.seconds(30),
        )

        # --- scaling -------------------------------------------------------------------------
        self.scaling = self.service.auto_scale_task_count(min_capacity=min_tasks, max_capacity=max_tasks)
        scale_in = Duration.seconds(env_int("CDK_AUTOSCALING_SCALE_IN_COOLDOWN", 300, minimum=0))
        scale_out = Duration.seconds(env_int("CDK_AUTOSCALING_SCALE_OUT_COOLDOWN", 60, minimum=0))
        # A target of 0 turns that policy off.
        cpu_target = env_int("CDK_AUTOSCALING_CPU_TARGET", 60, minimum=0)
        if cpu_target:
            self.scaling.scale_on_cpu_utilization(
                "CpuTarget", target_utilization_percent=cpu_target,
                scale_in_cooldown=scale_in, scale_out_cooldown=scale_out,
            )
        memory_target = env_int("CDK_AUTOSCALING_MEMORY_TARGET", 75, minimum=0)
        if memory_target:
            self.scaling.scale_on_memory_utilization(
                "MemoryTarget", target_utilization_percent=memory_target,
                scale_in_cooldown=scale_in, scale_out_cooldown=scale_out,
            )
        requests_target = env_int("CDK_AUTOSCALING_REQUESTS_PER_TARGET", 1000, minimum=0)
        if requests_target:
            self.scaling.scale_on_request_count(
                "RequestsTarget", requests_per_target=requests_target, target_group=self.target_group,
                scale_in_cooldown=scale_in, scale_out_cooldown=scale_out,
            )

        if env_bool("CDK_AUTOSCALING_SCHEDULE_ENABLED", True):
            time_zone = cdk.TimeZone.of(env_str("CDK_AUTOSCALING_TIMEZONE", "UTC"))
            peak_min = env_int("CDK_AUTOSCALING_PEAK_MIN", max(min_tasks, min(4, max_tasks)), minimum=0)
            if peak_min > max_tasks:
                raise ValueError(f"CDK_AUTOSCALING_PEAK_MIN={peak_min} is higher than CDK_AUTOSCALING_MAX={max_tasks}")
            self.scaling.scale_on_schedule(
                "PeakStart", min_capacity=peak_min, time_zone=time_zone,
                schedule=appscaling.Schedule.cron(
                    minute="0", hour=env_str("CDK_AUTOSCALING_PEAK_START_HOUR", "8"), week_day="MON-FRI"
                ),
            )
            self.scaling.scale_on_schedule(
                "PeakEnd", min_capacity=min_tasks, time_zone=time_zone,
                schedule=appscaling.Schedule.cron(
                    minute="0", hour=env_str("CDK_AUTOSCALING_PEAK_END_HOUR", "20"), week_day="MON-FRI"
                ),
            )

        # --- load generator (run on demand) ----------------------------------------------------
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
                "WORKERS": str(env_int("CDK_AUTOSCALING_LOAD_WORKERS", 20, minimum=1)),
                "DURATION": str(env_int("CDK_AUTOSCALING_LOAD_SECONDS", 600, minimum=1)),
            },
            logging=log_driver(self, config, f"{purpose}-load", "LoadLogGroup"),
        )
        self.load_security_group = ec2.SecurityGroup(
            self, "LoadSecurityGroup", vpc=self.vpc, description="load generator (egress only)"
        )

        cdk.CfnOutput(self, "Url", value=f"http://{self.alb.load_balancer_dns_name}:{port}/")
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        cdk.CfnOutput(self, "ServiceName", value=self.service.service_name)
        cdk.CfnOutput(self, "LoadTaskDefinitionArn", value=self.load_task.task_definition_arn)
        cdk.CfnOutput(
            self, "TaskSubnetIds",
            value=cdk.Fn.join(",", self.vpc.select_subnets(subnet_group_name=task_subnets(config).subnet_group_name).subnet_ids),
        )
        cdk.CfnOutput(self, "LoadSecurityGroupId", value=self.load_security_group.security_group_id)
        # ALBRequestCountPerTarget's ResourceLabel: app/<lb>/<id>/targetgroup/<tg>/<id>
        cdk.CfnOutput(
            self, "RequestCountResourceLabel",
            value=f"{self.alb.load_balancer_full_name}/{self.target_group.target_group_full_name}",
        )


STACK_CLASS = AutoscalingStack
