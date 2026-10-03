"""Module 03 - EC2 capacity: ECS on your own EC2 instances, scaled by a capacity provider.

AWS docs used while writing this module:
- aws_ecs README, "Clusters" and "Auto Scaling Group Capacity Providers" (the
  LaunchTemplate + AsgCapacityProvider pattern used here):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/README.html
- AsgCapacityProvider: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/AsgCapacityProvider.html
- Ec2Service: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/Ec2Service.html
- Amazon ECS capacity providers for EC2 (managed scaling, termination protection, draining):
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/asg-capacity-providers.html
- Task placement strategies: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-placement-strategies.html
- Service scheduler (REPLICA vs DAEMON): https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_services.html
- Amazon ECS container instance IAM role: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/instance_IAM_role.html
- Amazon ECS-optimized AMIs: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-optimized_AMI.html
- Docker Hub - prom/node-exporter (the daemon): https://hub.docker.com/r/prom/node-exporter

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Stack
from aws_cdk import aws_autoscaling as autoscaling
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from constructs import Construct

from shared.config import AppConfig, env_bool, env_int, env_str
from shared.ecs import desired_count, log_driver
from shared.naming import resource_name
from shared.network import build_vpc, task_subnets
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "Ec2CapacityStack"

DEFAULT_WEB_IMAGE = "nginx:1.30-alpine"
DEFAULT_DAEMON_IMAGE = "prom/node-exporter:v1.9.1"


class Ec2CapacityStack(Stack):
    """An ECS cluster whose capacity is an EC2 Auto Scaling group.

    - The ASG launches Amazon ECS-optimized Amazon Linux 2023 instances; the
      capacity provider's *managed scaling* grows/shrinks it to fit the
      tasks ECS needs to place (target `CDK_EC2_TARGET_CAPACITY` percent).
    - A REPLICA service (nginx) spreads its tasks across AZs, then packs
      them by memory onto as few instances as possible.
    - A DAEMON service (Prometheus node-exporter) runs exactly one task on
      every instance - the pattern for per-host agents.
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

        purpose = "ec2"
        self.vpc = build_vpc(self, config, purpose)

        cluster_name = resource_name(config.product, config.environment, "ecs", purpose)
        self.cluster = ecs.Cluster(self, "Cluster", cluster_name=cluster_name, vpc=self.vpc)
        apply_name_tag(self.cluster, cluster_name)

        # --- the instances -----------------------------------------------------
        # The container instance role: lets the ECS agent on each instance
        # register with the cluster, pull images and send logs (AWS-managed
        # policy), plus Session Manager access instead of SSH.
        instance_role = iam.Role(
            self,
            "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonEC2ContainerServiceforEC2Role"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )
        template_name = resource_name(config.product, config.environment, purpose, "instances")
        self.launch_template = ec2.LaunchTemplate(
            self,
            "LaunchTemplate",
            launch_template_name=template_name,
            instance_type=ec2.InstanceType(env_str("CDK_EC2_INSTANCE_TYPE", "t3.small")),
            machine_image=ecs.EcsOptimizedImage.amazon_linux2023(),
            # The capacity provider appends the "join this cluster" commands
            # (ECS_CLUSTER=... in /etc/ecs/ecs.config) to this user data.
            user_data=ec2.UserData.for_linux(),
            require_imdsv2=True,
            role=instance_role,
        )
        apply_name_tag(self.launch_template, template_name)

        asg_name = resource_name(config.product, config.environment, purpose, "asg")
        self.auto_scaling_group = autoscaling.AutoScalingGroup(
            self,
            "AutoScalingGroup",
            auto_scaling_group_name=asg_name,
            vpc=self.vpc,
            vpc_subnets=task_subnets(config),
            launch_template=self.launch_template,
            min_capacity=env_int("CDK_EC2_MIN_CAPACITY", 1, minimum=0),
            max_capacity=env_int("CDK_EC2_MAX_CAPACITY", 4, minimum=1),
        )
        apply_name_tag(self.auto_scaling_group, asg_name)

        provider_name = resource_name(config.product, config.environment, purpose, "asg-cp")
        self.capacity_provider = ecs.AsgCapacityProvider(
            self,
            "CapacityProvider",
            capacity_provider_name=provider_name,
            auto_scaling_group=self.auto_scaling_group,
            machine_image_type=ecs.MachineImageType.AMAZON_LINUX_2,
            enable_managed_scaling=True,
            target_capacity_percent=env_int("CDK_EC2_TARGET_CAPACITY", 100, minimum=1),
            # Off so `cdk destroy` can delete the ASG - see the CloudFormation
            # issue linked in the aws_ecs README (Notes and cautions).
            enable_managed_termination_protection=env_bool("CDK_EC2_TERMINATION_PROTECTION", False),
            enable_managed_draining=True,
            instance_warmup_period=env_int("CDK_EC2_WARMUP_SECONDS", 300, minimum=0),
        )
        self.cluster.add_asg_capacity_provider(self.capacity_provider)
        strategy = [ecs.CapacityProviderStrategy(capacity_provider=self.capacity_provider.capacity_provider_name, weight=1)]

        # --- a REPLICA service ---------------------------------------------------
        web_family = resource_name(config.product, config.environment, purpose, "web")
        web_task = ecs.Ec2TaskDefinition(
            self, "WebTaskDefinition", family=web_family, network_mode=ecs.NetworkMode.AWS_VPC
        )
        web_task.add_container(
            "web",
            container_name="web",
            image=ecs.ContainerImage.from_registry(env_str("CDK_EC2_WEB_IMAGE", DEFAULT_WEB_IMAGE)),
            memory_reservation_mib=128,
            cpu=128,
            port_mappings=[ecs.PortMapping(container_port=80)],
            logging=log_driver(self, config, f"{purpose}-web", "WebLogGroup"),
        )
        apply_name_tag(web_task, web_family)

        web_service_name = resource_name(config.product, config.environment, purpose, "web")
        self.web_service = ecs.Ec2Service(
            self,
            "WebService",
            service_name=web_service_name,
            cluster=self.cluster,
            task_definition=web_task,
            desired_count=desired_count("CDK_EC2_DESIRED_COUNT"),
            vpc_subnets=task_subnets(config),
            capacity_provider_strategies=strategy,
            min_healthy_percent=100,
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            # First one task per AZ, then fill instances by memory.
            placement_strategies=[
                ecs.PlacementStrategy.spread_across(ecs.BuiltInAttributes.AVAILABILITY_ZONE),
                ecs.PlacementStrategy.packed_by_memory(),
            ],
        )
        apply_name_tag(self.web_service, web_service_name)

        # --- a DAEMON service ------------------------------------------------------
        daemon_family = resource_name(config.product, config.environment, purpose, "node-exporter")
        daemon_task = ecs.Ec2TaskDefinition(
            self, "DaemonTaskDefinition", family=daemon_family, network_mode=ecs.NetworkMode.HOST
        )
        daemon_task.add_container(
            "node-exporter",
            container_name="node-exporter",
            image=ecs.ContainerImage.from_registry(env_str("CDK_EC2_DAEMON_IMAGE", DEFAULT_DAEMON_IMAGE)),
            memory_reservation_mib=64,
            cpu=64,
            port_mappings=[ecs.PortMapping(container_port=9100, host_port=9100)],
            logging=log_driver(self, config, f"{purpose}-node-exporter", "DaemonLogGroup"),
        )
        apply_name_tag(daemon_task, daemon_family)

        daemon_service_name = resource_name(config.product, config.environment, purpose, "node-exporter")
        self.daemon_service = ecs.Ec2Service(
            self,
            "DaemonService",
            service_name=daemon_service_name,
            cluster=self.cluster,
            task_definition=daemon_task,
            daemon=True,
            # A daemon must be able to stop its task on an instance before
            # starting the new one there.
            min_healthy_percent=0,
            max_healthy_percent=100,
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
        )
        apply_name_tag(self.daemon_service, daemon_service_name)

        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        cdk.CfnOutput(self, "AutoScalingGroupName", value=self.auto_scaling_group.auto_scaling_group_name)
        cdk.CfnOutput(self, "CapacityProviderName", value=self.capacity_provider.capacity_provider_name)


STACK_CLASS = Ec2CapacityStack
