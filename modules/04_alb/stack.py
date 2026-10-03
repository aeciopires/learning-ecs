"""Module 04 - ALB: an internet-facing and an internal Application Load Balancer.

AWS docs used while writing this module:
- aws_elasticloadbalancingv2 README ("Defining an Application Load Balancer",
  "Convenience methods and more complex Actions", "Setting up Access Log
  Bucket", "Configuring Health Checks"):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_elasticloadbalancingv2/README.html
- aws_ecs README, "Include an application/network load balancer":
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/README.html
- Use an Application Load Balancer for Amazon ECS:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/alb.html
- ALB listener rules: https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-rules.html
- ALB health checks: https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-health-checks.html
- ALB access logs: https://docs.aws.amazon.com/elasticloadbalancing/latest/application/enable-access-logging.html
- Docker Hub - traefik/whoami: https://hub.docker.com/r/traefik/whoami

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_s3 as s3
from constructs import Construct

from shared.config import AppConfig, env_bool, env_str
from shared.ecs import build_cluster, desired_count, fargate_service, listener_port
from shared.naming import bounded_name
from shared.network import PRIVATE, PUBLIC, build_vpc
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "AlbStack"

# Docker Hub image - re-check https://hub.docker.com/r/traefik/whoami for tags.
DEFAULT_IMAGE = "traefik/whoami:v1.12.0"


class AlbStack(Stack):
    """Two services, two ALBs: `web` on the internet, `api` only inside the VPC.

    - The internet-facing ALB lives in the public subnets and forwards to
      the `web` service; a listener rule answers `/admin*` with a fixed 403
      without ever reaching a task.
    - The internal ALB lives in the private subnets, has no public IP, and
      only accepts traffic from inside the VPC - the usual way to expose a
      backend to other services (module 08 shows Service Connect, the
      load-balancer-free alternative).
    - Optional: HTTPS with an ACM certificate (`CDK_ALB_CERTIFICATE_ARN`,
      real AWS only) and access logs to S3 (`CDK_ALB_ACCESS_LOGS=true`).
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

        self.vpc = build_vpc(self, config, "alb")
        self.cluster = build_cluster(self, config, self.vpc, "alb")
        image = env_str("CDK_ALB_IMAGE", DEFAULT_IMAGE)
        count = desired_count("CDK_ALB_DESIRED_COUNT")
        health_check = elbv2.HealthCheck(
            path="/health",
            healthy_http_codes="200",
            interval=Duration.seconds(15),
            timeout=Duration.seconds(5),
            healthy_threshold_count=2,
            unhealthy_threshold_count=3,
        )

        # --- the two services --------------------------------------------------
        self.web = fargate_service(
            self, config, self.cluster, construct_id="Web", purpose="alb-web", image=image,
            container_port=80, desired=count, environment={"WHOAMI_NAME": "web"},
            health_check_grace_period=Duration.seconds(30),
        )
        self.api = fargate_service(
            self, config, self.cluster, construct_id="Api", purpose="alb-api", image=image,
            container_port=80, desired=count, environment={"WHOAMI_NAME": "api"},
            health_check_grace_period=Duration.seconds(30),
        )

        # --- internet-facing ALB -> web -----------------------------------------
        public_name = bounded_name(config.product, config.environment, "alb", "public", max_length=32)
        self.public_alb = elbv2.ApplicationLoadBalancer(
            self,
            "PublicAlb",
            load_balancer_name=public_name,
            vpc=self.vpc,
            internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=PUBLIC),
            drop_invalid_header_fields=True,
            idle_timeout=Duration.seconds(60),
        )
        apply_name_tag(self.public_alb, public_name)

        certificate_arn = env_str("CDK_ALB_CERTIFICATE_ARN", "")
        if certificate_arn:
            # HTTPS on 443, and plain HTTP redirected to it.
            self.public_listener = self.public_alb.add_listener(
                "Https",
                port=443,
                certificates=[acm.Certificate.from_certificate_arn(self, "Certificate", certificate_arn)],
                ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
                open=True,
            )
            self.public_alb.add_redirect(source_port=listener_port("ALB_PUBLIC"), target_port=443)
        else:
            self.public_listener = self.public_alb.add_listener(
                "Http", port=listener_port("ALB_PUBLIC"), protocol=elbv2.ApplicationProtocol.HTTP, open=True
            )

        self.web_target_group = self.public_listener.add_targets(
            "Web",
            target_group_name=bounded_name(config.product, config.environment, "tg", "alb-web", max_length=32),
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[self.web],
            health_check=health_check,
            deregistration_delay=Duration.seconds(30),
        )
        # A rule: requests to /admin* never reach a task.
        self.public_listener.add_action(
            "BlockAdmin",
            priority=10,
            conditions=[elbv2.ListenerCondition.path_patterns(["/admin", "/admin/*"])],
            action=elbv2.ListenerAction.fixed_response(403, content_type="text/plain", message_body="forbidden"),
        )

        # --- internal ALB -> api --------------------------------------------------
        internal_name = bounded_name(config.product, config.environment, "alb", "internal", max_length=32)
        self.internal_alb = elbv2.ApplicationLoadBalancer(
            self,
            "InternalAlb",
            load_balancer_name=internal_name,
            vpc=self.vpc,
            internet_facing=False,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=PRIVATE if config.nat_gateways else PUBLIC),
            drop_invalid_header_fields=True,
        )
        apply_name_tag(self.internal_alb, internal_name)
        internal_port = listener_port("ALB_INTERNAL")
        # open=False: no 0.0.0.0/0 rule - only the VPC's own range may connect.
        self.internal_listener = self.internal_alb.add_listener(
            "Http", port=internal_port, protocol=elbv2.ApplicationProtocol.HTTP, open=False
        )
        self.internal_alb.connections.allow_from(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block), ec2.Port.tcp(internal_port), "HTTP from inside the VPC"
        )
        self.api_target_group = self.internal_listener.add_targets(
            "Api",
            target_group_name=bounded_name(config.product, config.environment, "tg", "alb-api", max_length=32),
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[self.api],
            health_check=health_check,
            deregistration_delay=Duration.seconds(30),
        )

        # --- optional access logs ----------------------------------------------------
        if env_bool("CDK_ALB_ACCESS_LOGS", False):
            bucket = s3.Bucket(
                self,
                "AccessLogsBucket",
                encryption=s3.BucketEncryption.S3_MANAGED,  # the only encryption ALB logs support
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                enforce_ssl=True,
                removal_policy=cdk.RemovalPolicy.DESTROY,
                auto_delete_objects=True,
            )
            self.public_alb.log_access_logs(bucket, prefix="public")
            self.internal_alb.log_access_logs(bucket, prefix="internal")

        scheme = "https" if certificate_arn else "http"
        cdk.CfnOutput(self, "PublicUrl", value=f"{scheme}://{self.public_alb.load_balancer_dns_name}:"
                      f"{443 if certificate_arn else listener_port('ALB_PUBLIC')}/")
        cdk.CfnOutput(self, "InternalUrl", value=f"http://{self.internal_alb.load_balancer_dns_name}:{internal_port}/")
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)


STACK_CLASS = AlbStack
