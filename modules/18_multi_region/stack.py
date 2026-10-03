"""Module 18 - Multi-region: the same service in two regions, CloudFront origin failover in front.

AWS docs used while writing this module:
- AWS CDK environments (one stack per account/region):
  https://docs.aws.amazon.com/cdk/v2/guide/environments.html
- aws_cloudfront_origins README ("Failover Origins"):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_cloudfront_origins/README.html
- Optimize high availability with CloudFront origin failover:
  https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/high_availability_origin_failover.html
- Docker Hub - traefik/whoami: https://hub.docker.com/r/traefik/whoami

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from constructs import Construct

from shared.config import AppConfig, env_int, env_str
from shared.ecs import build_cluster, desired_count, fargate_service, listener_port
from shared.naming import bounded_name, resource_name
from shared.network import PUBLIC, build_vpc
from shared.tagging import apply_name_tag, apply_standard_tags

# build_stacks() creates these three; STACK_ID is the first, for the module contract.
STACK_ID = "MultiRegionPrimaryStack"
SECONDARY_STACK_ID = "MultiRegionSecondaryStack"
GLOBAL_STACK_ID = "MultiRegionGlobalStack"

DEFAULT_IMAGE = "traefik/whoami:v1.12.0"
ROLES = ("primary", "secondary")
# Status codes CloudFront may fail over on: 400, 403, 404, 416, 429, 500, 502, 503, 504.
FAILOVER_STATUS_CODES = [500, 502, 503, 504]


class MultiRegionStack(Stack):
    """One region's copy of the application: VPC, cluster, ALB, service.

    Both regions get identical names (names are per region); each task
    answers with its region in `Name:`, so you can see which one served a
    request.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        role: str = "primary",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if role not in ROLES:
            raise ValueError(f"role={role!r}: must be one of {', '.join(ROLES)}")
        apply_standard_tags(self, tags=config.to_standard_tags())

        purpose = "multi-region"
        self.vpc = build_vpc(self, config, purpose)
        self.cluster = build_cluster(self, config, self.vpc, purpose)
        self.service = fargate_service(
            self, config, self.cluster, construct_id="Web", purpose=f"{purpose}-web",
            image=env_str("CDK_MULTI_REGION_IMAGE", DEFAULT_IMAGE), container_port=80,
            desired=desired_count("CDK_MULTI_REGION_DESIRED_COUNT"),
            # The region (a token resolved at deploy time) and the role, in every response.
            environment={"WHOAMI_NAME": f"{role} {self.region}"},
            health_check_grace_period=Duration.seconds(30),
        )

        alb_name = bounded_name(config.product, config.environment, "alb", purpose, max_length=32)
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=alb_name, vpc=self.vpc, internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=PUBLIC),
        )
        apply_name_tag(self.alb, alb_name)
        port = listener_port("MULTI_REGION" if role == "primary" else "MULTI_REGION_SECONDARY")
        listener = self.alb.add_listener("Http", port=port, protocol=elbv2.ApplicationProtocol.HTTP, open=True)
        listener.add_targets(
            "Web",
            target_group_name=bounded_name(config.product, config.environment, "tg", purpose, max_length=32),
            port=80, protocol=elbv2.ApplicationProtocol.HTTP, targets=[self.service],
            health_check=elbv2.HealthCheck(path="/health", healthy_http_codes="200", interval=Duration.seconds(10)),
            deregistration_delay=Duration.seconds(30),
        )

        cdk.CfnOutput(self, "AlbDnsName", value=self.alb.load_balancer_dns_name)
        cdk.CfnOutput(self, "Url", value=f"http://{self.alb.load_balancer_dns_name}:{port}/")
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        cdk.CfnOutput(self, "ServiceName", value=self.service.service_name)


class MultiRegionGlobalStack(Stack):
    """A CloudFront distribution whose origin is an origin group: primary
    region first, secondary region when the primary fails.

    The origins are plain domain names (the two ALBs' DNS names - outputs of
    the regional stacks), passed in through CDK_MULTI_REGION_*_ORIGIN.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        primary_origin: str,
        secondary_origin: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        apply_standard_tags(self, tags=config.to_standard_tags())

        def origin(domain: str, role: str) -> origins.HttpOrigin:
            return origins.HttpOrigin(
                domain,
                protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                http_port=env_int(f"CDK_MULTI_REGION_{role.upper()}_ORIGIN_PORT", 80, minimum=1),
                # Fail over sooner than the defaults (3 attempts x 10 s).
                connection_attempts=env_int("CDK_MULTI_REGION_CONNECTION_ATTEMPTS", 1, minimum=1),
                connection_timeout=Duration.seconds(env_int("CDK_MULTI_REGION_CONNECTION_TIMEOUT", 3, minimum=1)),
                read_timeout=Duration.seconds(env_int("CDK_MULTI_REGION_READ_TIMEOUT", 10, minimum=1)),
            )

        self.origin_group = origins.OriginGroup(
            primary_origin=origin(primary_origin, "primary"),
            fallback_origin=origin(secondary_origin, "secondary"),
            fallback_status_codes=FAILOVER_STATUS_CODES,
        )
        name = resource_name(config.product, config.environment, "cf", "multi-region")
        self.distribution = cloudfront.Distribution(
            self, "Distribution",
            comment=name,
            default_behavior=cloudfront.BehaviorOptions(
                origin=self.origin_group,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                # Failover only happens for GET, HEAD and OPTIONS (OPTIONS only if cached).
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
            ),
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )
        apply_name_tag(self.distribution, name)

        cdk.CfnOutput(self, "DistributionId", value=self.distribution.distribution_id)
        cdk.CfnOutput(self, "DistributionUrl", value=f"https://{self.distribution.distribution_domain_name}/")


def build_stacks(app: cdk.App, *, config: AppConfig, env: cdk.Environment | None) -> list[Stack]:
    """Two regional stacks, plus the global one once both origins are known.

    Regions: CDK_MULTI_REGION_PRIMARY (default: the app's region, else
    us-east-1) and CDK_MULTI_REGION_SECONDARY (default us-west-2).
    """
    account = env.account if env else None
    primary_region = env_str("CDK_MULTI_REGION_PRIMARY", (env.region if env else None) or "us-east-1")
    secondary_region = env_str("CDK_MULTI_REGION_SECONDARY", "us-west-2")
    if primary_region == secondary_region:
        raise ValueError(f"CDK_MULTI_REGION_PRIMARY and CDK_MULTI_REGION_SECONDARY are both {primary_region!r}")

    stacks: list[Stack] = [
        MultiRegionStack(app, STACK_ID, config=config, role="primary",
                         env=cdk.Environment(account=account, region=primary_region)),
        MultiRegionStack(app, SECONDARY_STACK_ID, config=config, role="secondary",
                         env=cdk.Environment(account=account, region=secondary_region)),
    ]
    primary_origin = env_str("CDK_MULTI_REGION_PRIMARY_ORIGIN", "")
    secondary_origin = env_str("CDK_MULTI_REGION_SECONDARY_ORIGIN", "")
    if primary_origin and secondary_origin:
        # CloudFront is global; its stack lives in the primary region.
        stacks.append(MultiRegionGlobalStack(
            app, GLOBAL_STACK_ID, config=config, primary_origin=primary_origin, secondary_origin=secondary_origin,
            env=cdk.Environment(account=account, region=primary_region),
        ))
    return stacks


STACK_CLASS = MultiRegionStack
