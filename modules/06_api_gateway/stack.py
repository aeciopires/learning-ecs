"""Module 06 - API Gateway in front of an ECS service (REST API or HTTP API, via a VPC link).

AWS docs used while writing this module:
- aws_apigateway README, "Private Integrations" (VpcLink + NLB):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_apigateway/README.html
- aws_apigatewayv2_integrations README, "Private Integration" (HttpAlbIntegration):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_apigatewayv2_integrations/README.html
- REST API private integrations (VPC link to an NLB):
  https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-private-integration.html
- HTTP API private integrations (VPC link to an ALB/NLB/Cloud Map):
  https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-develop-integrations-private.html
- Choosing between REST APIs and HTTP APIs:
  https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html
- REST API throttling and access logging:
  https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-request-throttling.html
  https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-logging.html

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

from typing import cast

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_logs as logs
from constructs import Construct

from shared.config import AppConfig, env_bool, env_int, env_str
from shared.ecs import build_cluster, desired_count, fargate_service, listener_port, retention
from shared.naming import bounded_name, resource_name
from shared.network import PRIVATE, PUBLIC, build_vpc
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "ApiGatewayStack"

DEFAULT_IMAGE = "traefik/whoami:v1.12.0"
API_TYPES = ("rest", "http")
CONNECTIONS = ("vpc_link", "internet")


class ApiGatewayStack(Stack):
    """API Gateway -> load balancer -> Fargate service, two ways.

    CDK_APIGW_TYPE:
      - `rest` (default): a REST API with a `{proxy+}` resource, whose VPC
        link (the CDK L2 `apigateway.VpcLink`, a "VPC link V1") targets an
        **NLB**. REST APIs can also use a VPC link V2 to an ALB or NLB -
        the L2 construct doesn't expose that yet; see the README.
      - `http`: an HTTP API whose VPC link targets an **ALB** listener.

    CDK_APIGW_CONNECTION:
      - `vpc_link` (default): the load balancer is internal - API Gateway
        reaches it privately through a VPC link; nothing else can.
      - `internet`: the load balancer is internet-facing and API Gateway
        calls its public URL. floci 2.1.0 implements no VPC links, so
        .env.example uses this mode - see README, "floci vs real AWS".
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

        api_type = env_str("CDK_APIGW_TYPE", "rest").lower()
        connection = env_str("CDK_APIGW_CONNECTION", "vpc_link").lower()
        if api_type not in API_TYPES:
            raise ValueError(f"CDK_APIGW_TYPE={api_type!r}: must be one of {', '.join(API_TYPES)}")
        if connection not in CONNECTIONS:
            raise ValueError(f"CDK_APIGW_CONNECTION={connection!r}: must be one of {', '.join(CONNECTIONS)}")
        private = connection == "vpc_link"

        self.vpc = build_vpc(self, config, "apigw")
        self.cluster = build_cluster(self, config, self.vpc, "apigw")
        self.service = fargate_service(
            self, config, self.cluster, construct_id="Api", purpose="apigw-api",
            image=env_str("CDK_APIGW_IMAGE", DEFAULT_IMAGE), container_port=80,
            desired=desired_count("CDK_APIGW_DESIRED_COUNT"), environment={"WHOAMI_NAME": "behind-api-gateway"},
            health_check_grace_period=Duration.seconds(30),
        )
        port = listener_port("API_GATEWAY")
        lb_subnets = ec2.SubnetSelection(
            subnet_group_name=PRIVATE if (private and config.nat_gateways) else PUBLIC
        )
        lb_name = bounded_name(config.product, config.environment, "apigw", api_type, max_length=32)
        api_name = resource_name(config.product, config.environment, "apigw", api_type)

        # REST API VPC links target an NLB; HTTP API VPC links reach the ALB's listener.
        self.load_balancer: elbv2.NetworkLoadBalancer | elbv2.ApplicationLoadBalancer
        if api_type == "rest":
            self.load_balancer = self._nlb(config, lb_name, lb_subnets, private, port)
            self.url = self._rest_api(config, api_name, private, port)
        else:
            self.load_balancer = self._alb(config, lb_name, lb_subnets, private, port)
            self.url = self._http_api(api_name, private, port)

        cdk.CfnOutput(self, "ApiUrl", value=self.url)
        cdk.CfnOutput(self, "LoadBalancerDns", value=self.load_balancer.load_balancer_dns_name)

    # --- load balancers ------------------------------------------------------------

    def _nlb(self, config, name, subnets, private, port) -> elbv2.NetworkLoadBalancer:
        security_group = ec2.SecurityGroup(self, "NlbSecurityGroup", vpc=self.vpc, allow_all_outbound=False)
        # A VPC link reaches the NLB from inside the VPC.
        security_group.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block) if private else ec2.Peer.any_ipv4(), ec2.Port.tcp(port)
        )
        security_group.add_egress_rule(ec2.Peer.ipv4(self.vpc.vpc_cidr_block), ec2.Port.tcp(80))
        nlb = elbv2.NetworkLoadBalancer(
            self, "Nlb", load_balancer_name=name, vpc=self.vpc, internet_facing=not private,
            vpc_subnets=subnets, security_groups=[security_group], cross_zone_enabled=True,
        )
        apply_name_tag(nlb, name)
        self.service.connections.allow_from(security_group, ec2.Port.tcp(80))
        nlb.add_listener("Tcp", port=port, protocol=elbv2.Protocol.TCP).add_targets(
            "Tasks", port=80, protocol=elbv2.Protocol.TCP, targets=[self.service],
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(protocol=elbv2.Protocol.HTTP, path="/health"),
        )
        return nlb

    def _alb(self, config, name, subnets, private, port) -> elbv2.ApplicationLoadBalancer:
        alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=name, vpc=self.vpc, internet_facing=not private, vpc_subnets=subnets,
        )
        apply_name_tag(alb, name)
        self.alb_listener = alb.add_listener(
            "Http", port=port, protocol=elbv2.ApplicationProtocol.HTTP, open=not private
        )
        if private:
            alb.connections.allow_from(ec2.Peer.ipv4(self.vpc.vpc_cidr_block), ec2.Port.tcp(port))
        self.alb_listener.add_targets(
            "Tasks", port=80, targets=[self.service], deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(path="/health"),
        )
        return alb

    # --- APIs ---------------------------------------------------------------------------

    def _backend_url(self, port: int) -> str:
        """Where API Gateway sends requests: the load balancer, unless
        CDK_APIGW_BACKEND_URL overrides it (e.g. a custom domain on the load
        balancer - or, on floci, `http://localhost:<port>`, because floci's own
        API Gateway can't resolve the *.elb.floci names it hands out)."""
        return env_str("CDK_APIGW_BACKEND_URL", f"http://{self.load_balancer.load_balancer_dns_name}:{port}").rstrip("/")

    def _rest_api(self, config, api_name, private, port) -> str:
        if private:
            vpc_link_name = resource_name(config.product, config.environment, "apigw", "vpc-link")
            vpc_link = apigateway.VpcLink(
                self, "VpcLink", vpc_link_name=vpc_link_name,
                targets=[cast(elbv2.NetworkLoadBalancer, self.load_balancer)]
            )
            apply_name_tag(vpc_link, vpc_link_name)
            connection: dict = {"connection_type": apigateway.ConnectionType.VPC_LINK, "vpc_link": vpc_link}
        else:
            connection = {"connection_type": apigateway.ConnectionType.INTERNET}
        base = self._backend_url(port)

        def proxy_integration(path: str, mapped: bool) -> apigateway.Integration:
            return apigateway.Integration(
                type=apigateway.IntegrationType.HTTP_PROXY,
                integration_http_method="ANY",
                uri=f"{base}{path}",
                options=apigateway.IntegrationOptions(
                    timeout=Duration.seconds(29),
                    request_parameters={"integration.request.path.proxy": "method.request.path.proxy"}
                    if mapped else None,
                    **connection,
                ),
            )

        stage_options: dict = {
            "stage_name": config.environment,
            "throttling_rate_limit": env_int("CDK_APIGW_RATE_LIMIT", 100, minimum=1),
            "throttling_burst_limit": env_int("CDK_APIGW_BURST_LIMIT", 200, minimum=1),
            # Detailed (per-method) CloudWatch metrics bill extra - opt-in.
            "metrics_enabled": env_bool("CDK_APIGW_DETAILED_METRICS", False),
        }
        access_logs = env_bool("CDK_APIGW_ACCESS_LOGS", True)
        if access_logs:
            log_group_name = f"/apigateway/{config.product}/{config.environment}/{api_name}"
            log_group = logs.LogGroup(
                self, "AccessLogs", log_group_name=log_group_name, retention=retention(config),
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            apply_name_tag(log_group, log_group_name)
            stage_options.update(
                access_log_destination=apigateway.LogGroupLogDestination(log_group),
                access_log_format=apigateway.AccessLogFormat.json_with_standard_fields(
                    caller=False, http_method=True, ip=True, protocol=True, request_time=True,
                    resource_path=True, response_length=True, status=True, user=False,
                ),
            )

        self.rest_api = apigateway.RestApi(
            self,
            "RestApi",
            rest_api_name=api_name,
            description="REST API in front of an ECS service (learning-ecs module 06)",
            endpoint_types=[apigateway.EndpointType.REGIONAL],
            deploy_options=apigateway.StageOptions(**stage_options),
            # Access logging needs the account-level CloudWatch role.
            cloud_watch_role=access_logs,
            cloud_watch_role_removal_policy=cdk.RemovalPolicy.DESTROY if access_logs else None,
        )
        apply_name_tag(self.rest_api, api_name)
        self.rest_api.root.add_method("ANY", proxy_integration("/", mapped=False))
        self.rest_api.root.add_resource("{proxy+}").add_method(
            "ANY",
            proxy_integration("/{proxy}", mapped=True),
            request_parameters={"method.request.path.proxy": True},
        )
        return self.rest_api.url

    def _http_api(self, api_name, private, port) -> str:
        if private:
            integration: apigwv2.HttpRouteIntegration = integrations.HttpAlbIntegration(
                "AlbIntegration", self.alb_listener
            )
        else:
            integration = integrations.HttpUrlIntegration("UrlIntegration", f"{self._backend_url(port)}/{{proxy}}")
        self.http_api = apigwv2.HttpApi(self, "HttpApi", api_name=api_name, create_default_stage=True)
        apply_name_tag(self.http_api, api_name)
        self.http_api.add_routes(path="/{proxy+}", methods=[apigwv2.HttpMethod.ANY], integration=integration)
        return self.http_api.api_endpoint


STACK_CLASS = ApiGatewayStack
