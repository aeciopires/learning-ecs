"""Module 07 - CloudFront in front of an ECS service (public ALB origin, or VPC origin).

AWS docs used while writing this module:
- aws_cloudfront_origins README ("ELBv2 Load Balancer", "From an HTTP endpoint",
  "VPC origins", "Restrict traffic coming to the VPC origin"):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_cloudfront_origins/README.html
- Distribution: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_cloudfront/Distribution.html
- Restrict access to Application Load Balancers (custom header + managed prefix list):
  https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/restrict-access-to-load-balancer.html
- Restrict access with VPC origins:
  https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html
- Managed cache policies / origin request policies:
  https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html
  https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-origin-request-policies.html

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, SecretValue, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from shared.config import AppConfig, env_str
from shared.ecs import build_cluster, desired_count, fargate_service, listener_port
from shared.naming import bounded_name, resource_name
from shared.network import PRIVATE, PUBLIC, build_vpc
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "CloudFrontStack"

DEFAULT_IMAGE = "traefik/whoami:v1.12.0"
ORIGIN_MODES = ("public_alb", "vpc_origin")
PRICE_CLASSES = {
    "100": cloudfront.PriceClass.PRICE_CLASS_100,
    "200": cloudfront.PriceClass.PRICE_CLASS_200,
    "all": cloudfront.PriceClass.PRICE_CLASS_ALL,
}
# The header CloudFront adds to every origin request in public_alb mode.
ORIGIN_HEADER = "X-Origin-Verify"


class CloudFrontStack(Stack):
    """CloudFront -> ALB -> Fargate service.

    CDK_CLOUDFRONT_ORIGIN:
      - `public_alb` (default): an internet-facing ALB. CloudFront adds a
        secret header to every origin request; the ALB forwards only
        requests that carry it and answers 403 to everything else (direct
        hits that bypass CloudFront). With CDK_CLOUDFRONT_PREFIX_LIST_ID the
        ALB's security group also only admits CloudFront's origin-facing
        addresses.
      - `vpc_origin`: an internal ALB in private subnets, reached through a
        CloudFront VPC origin - no public entry point at all. Needs
        CDK_CLOUDFRONT_PREFIX_LIST_ID (the ALB must admit CloudFront).

    Two cache behaviors: the default one doesn't cache (dynamic content,
    every header/cookie/query string forwarded), `/static/*` uses the
    managed CachingOptimized policy.
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

        mode = env_str("CDK_CLOUDFRONT_ORIGIN", "public_alb").lower()
        if mode not in ORIGIN_MODES:
            raise ValueError(f"CDK_CLOUDFRONT_ORIGIN={mode!r}: must be one of {', '.join(ORIGIN_MODES)}")
        price_class = env_str("CDK_CLOUDFRONT_PRICE_CLASS", "100").lower()
        if price_class not in PRICE_CLASSES:
            raise ValueError(f"CDK_CLOUDFRONT_PRICE_CLASS={price_class!r}: must be one of {', '.join(PRICE_CLASSES)}")
        prefix_list_id = env_str("CDK_CLOUDFRONT_PREFIX_LIST_ID", "")
        if mode == "vpc_origin" and not prefix_list_id:
            raise ValueError(
                "CDK_CLOUDFRONT_ORIGIN=vpc_origin needs CDK_CLOUDFRONT_PREFIX_LIST_ID - the id of the "
                "com.amazonaws.global.cloudfront.origin-facing managed prefix list (see this module's README)"
            )

        self.vpc = build_vpc(self, config, "cloudfront")
        self.cluster = build_cluster(self, config, self.vpc, "cloudfront")
        self.service = fargate_service(
            self, config, self.cluster, construct_id="Web", purpose="cloudfront-web",
            image=env_str("CDK_CLOUDFRONT_IMAGE", DEFAULT_IMAGE), container_port=80,
            desired=desired_count("CDK_CLOUDFRONT_DESIRED_COUNT"), environment={"WHOAMI_NAME": "behind-cloudfront"},
            health_check_grace_period=Duration.seconds(30),
        )

        public = mode == "public_alb"
        port = listener_port("CLOUDFRONT_ALB")
        alb_name = bounded_name(config.product, config.environment, "cf", "alb", max_length=32)
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=alb_name, vpc=self.vpc, internet_facing=public,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=PUBLIC if public or not config.nat_gateways else PRIVATE),
        )
        apply_name_tag(self.alb, alb_name)
        # Who may reach the ALB at the network level.
        self.listener = self.alb.add_listener(
            "Http", port=port, protocol=elbv2.ApplicationProtocol.HTTP, open=public and not prefix_list_id,
        )
        if prefix_list_id:
            self.alb.connections.allow_from(
                ec2.Peer.prefix_list(prefix_list_id), ec2.Port.tcp(port), "CloudFront origin-facing servers"
            )
        self.listener.add_targets(
            "Web", port=80, targets=[self.service], deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(path="/health"), priority=10 if public else None,
            conditions=[elbv2.ListenerCondition.http_header(ORIGIN_HEADER, [self._origin_secret(config)])]
            if public else None,
        )
        if public:
            # Default action: anything without the secret header (a direct hit
            # on the ALB, bypassing CloudFront) gets a 403.
            self.listener.add_action(
                "Default",
                action=elbv2.ListenerAction.fixed_response(403, content_type="text/plain", message_body="forbidden"),
            )

        origin = self._origin(public, port)
        distribution_comment = resource_name(config.product, config.environment, "cloudfront", "web")
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=distribution_comment,
            price_class=PRICE_CLASSES[price_class],
            default_behavior=cloudfront.BehaviorOptions(
                origin=origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
            additional_behaviors={
                "/static/*": cloudfront.BehaviorOptions(
                    origin=origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                    compress=True,
                ),
            },
        )
        apply_name_tag(self.distribution, distribution_comment)

        cdk.CfnOutput(self, "DistributionId", value=self.distribution.distribution_id)
        cdk.CfnOutput(self, "DistributionUrl", value=f"https://{self.distribution.distribution_domain_name}/")
        cdk.CfnOutput(self, "AlbDns", value=self.alb.load_balancer_dns_name)

    def _origin_secret(self, config: AppConfig) -> str:
        """A generated secret, referenced *by name* - CloudFormation resolves it
        at deploy time, so the value never appears in the template (the same
        pattern, and the same floci reason, as REQUIREMENTS.md section 10 gives
        for database passwords)."""
        secret_name = resource_name(config.product, config.environment, "cloudfront", "origin-verify")
        self.origin_secret_name = secret_name
        self.origin_secret = secretsmanager.Secret(
            self, "OriginVerifySecret", secret_name=secret_name,
            generate_secret_string=secretsmanager.SecretStringGenerator(exclude_punctuation=True, password_length=32),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        apply_name_tag(self.origin_secret, secret_name)
        return SecretValue.secrets_manager(secret_name).unsafe_unwrap()

    def _origin(self, public: bool, port: int) -> cloudfront.IOrigin:
        if not public:
            return origins.VpcOrigin.with_application_load_balancer(
                self.alb, http_port=port, protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            )
        custom_headers = {ORIGIN_HEADER: SecretValue.secrets_manager(self.origin_secret_name).unsafe_unwrap()}
        domain_override = env_str("CDK_CLOUDFRONT_ORIGIN_DOMAIN", "")
        if domain_override:
            # floci: its CloudFront can't resolve *.elb.floci names - see README.
            return origins.HttpOrigin(
                domain_override, http_port=port, protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                custom_headers=custom_headers,
            )
        return origins.LoadBalancerV2Origin(
            self.alb, http_port=port, protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            custom_headers=custom_headers,
        )


STACK_CLASS = CloudFrontStack
