"""Module 05 - NLB: an internet-facing and an internal Network Load Balancer (TCP).

AWS docs used while writing this module:
- aws_elasticloadbalancingv2 README ("Defining a Network Load Balancer",
  "Security Groups for Network Load Balancer", "Network Load Balancer attributes"):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_elasticloadbalancingv2/README.html
- Use a Network Load Balancer for Amazon ECS:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/nlb.html
- Network Load Balancers: https://docs.aws.amazon.com/elasticloadbalancing/latest/network/introduction.html
- Security groups for your Network Load Balancer:
  https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-security-groups.html
- Target groups (client IP preservation, health checks):
  https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-target-groups.html
- Docker Hub - traefik/whoami: https://hub.docker.com/r/traefik/whoami

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from constructs import Construct

from shared.config import AppConfig, env_bool, env_str
from shared.ecs import build_cluster, desired_count, fargate_service, listener_port
from shared.naming import bounded_name
from shared.network import PRIVATE, PUBLIC, build_vpc
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "NlbStack"

DEFAULT_IMAGE = "traefik/whoami:v1.12.0"


class NlbStack(Stack):
    """Two TCP services behind two NLBs: one public, one internal.

    A Network Load Balancer works at layer 4 (TCP/UDP/TLS): no paths, no
    headers, but static IPs per AZ, very high throughput, long-lived
    connections, client IP preservation - and it is what API Gateway's
    REST API VPC links target (module 06). Each NLB here has its own
    security group (the modern default), so the task security group only
    has to trust the NLB's group.
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

        self.vpc = build_vpc(self, config, "nlb")
        self.cluster = build_cluster(self, config, self.vpc, "nlb")
        image = env_str("CDK_NLB_IMAGE", DEFAULT_IMAGE)
        count = desired_count("CDK_NLB_DESIRED_COUNT")
        cross_zone = env_bool("CDK_NLB_CROSS_ZONE", True)

        self.public_service = fargate_service(
            self, config, self.cluster, construct_id="Public", purpose="nlb-public", image=image,
            container_port=80, desired=count, environment={"WHOAMI_NAME": "nlb-public"},
            health_check_grace_period=Duration.seconds(30),
        )
        self.internal_service = fargate_service(
            self, config, self.cluster, construct_id="Internal", purpose="nlb-internal", image=image,
            container_port=80, desired=count, environment={"WHOAMI_NAME": "nlb-internal"},
            health_check_grace_period=Duration.seconds(30),
        )

        public_port = listener_port("NLB_PUBLIC")
        internal_port = listener_port("NLB_INTERNAL")

        self.public_nlb, self.public_target_group = self._nlb(
            config, "Public", "public", internet_facing=True, port=public_port, cross_zone=cross_zone,
            subnet_group=PUBLIC, service=self.public_service,
            allowed=ec2.Peer.any_ipv4(),
        )
        self.internal_nlb, self.internal_target_group = self._nlb(
            config, "Internal", "internal", internet_facing=False, port=internal_port, cross_zone=cross_zone,
            subnet_group=PRIVATE if config.nat_gateways else PUBLIC, service=self.internal_service,
            allowed=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
        )

        cdk.CfnOutput(self, "PublicEndpoint", value=f"{self.public_nlb.load_balancer_dns_name}:{public_port}")
        cdk.CfnOutput(self, "InternalEndpoint", value=f"{self.internal_nlb.load_balancer_dns_name}:{internal_port}")
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)

    def _nlb(
        self,
        config: AppConfig,
        construct_id: str,
        scheme: str,
        *,
        internet_facing: bool,
        port: int,
        cross_zone: bool,
        subnet_group: str,
        service,
        allowed: ec2.IPeer,
    ) -> tuple[elbv2.NetworkLoadBalancer, elbv2.NetworkTargetGroup]:
        name = bounded_name(config.product, config.environment, "nlb", scheme, max_length=32)
        security_group = ec2.SecurityGroup(
            self, f"{construct_id}NlbSecurityGroup", vpc=self.vpc,
            description=f"{name}: who may connect to the NLB", allow_all_outbound=False,
        )
        security_group.add_ingress_rule(allowed, ec2.Port.tcp(port), "clients of the NLB")
        nlb = elbv2.NetworkLoadBalancer(
            self,
            f"{construct_id}Nlb",
            load_balancer_name=name,
            vpc=self.vpc,
            internet_facing=internet_facing,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=subnet_group),
            security_groups=[security_group],
            cross_zone_enabled=cross_zone,
        )
        apply_name_tag(nlb, name)
        # NLB -> tasks: open the task security group to the NLB's group only,
        # and let the NLB's group reach the tasks.
        service.connections.allow_from(security_group, ec2.Port.tcp(80), f"from {name}")
        security_group.add_egress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block), ec2.Port.tcp(80), "to the tasks (and their health checks)"
        )
        listener = nlb.add_listener("Tcp", port=port, protocol=elbv2.Protocol.TCP)
        target_group = listener.add_targets(
            "Tasks",
            target_group_name=bounded_name(config.product, config.environment, "tg", f"nlb-{scheme}", max_length=32),
            port=80,
            protocol=elbv2.Protocol.TCP,
            targets=[service],
            deregistration_delay=Duration.seconds(30),
            # An HTTP health check is more meaningful than "the port is open".
            health_check=elbv2.HealthCheck(
                protocol=elbv2.Protocol.HTTP, path="/health", interval=Duration.seconds(10),
                healthy_threshold_count=2, unhealthy_threshold_count=2,
            ),
        )
        return nlb, target_group


STACK_CLASS = NlbStack
