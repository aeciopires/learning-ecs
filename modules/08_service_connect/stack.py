"""Module 08 - Service Connect: service-to-service calls by name, without a load balancer.

AWS docs used while writing this module:
- aws_ecs README, "Amazon ECS Service Connect" and "Service Connect Access Logs":
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/README.html
- Use Service Connect to connect Amazon ECS services with short names:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-connect.html
- Service Connect concepts (namespace, client/client-server, discovery and client aliases):
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-connect-concepts.html
- Service Connect access logs: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-connect-envoy-access-logs.html
- Docker Hub - nginx (the frontend, as a reverse proxy) and traefik/whoami (the API):
  https://hub.docker.com/_/nginx  https://hub.docker.com/r/traefik/whoami

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_servicediscovery as servicediscovery
from constructs import Construct

from shared.config import AppConfig, env_str
from shared.ecs import build_cluster, desired_count, fargate_service, listener_port, log_driver
from shared.naming import bounded_name
from shared.network import build_vpc
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "ServiceConnectStack"

API_IMAGE = "traefik/whoami:v1.12.0"
FRONTEND_IMAGE = "nginx:1.30-alpine"
# The short name other services use to call the API, through Service Connect.
API_DNS_NAME = "api"

# nginx as a reverse proxy to $UPSTREAM_URL. Written at start-up so the image
# stays the unmodified official one (no custom build, no extra files).
NGINX_START = (
    'printf \'server {\\n  listen 80;\\n  location /health { return 200 "ok"; }\\n'
    '  location / { proxy_pass %s; proxy_set_header Host $host; }\\n}\\n\' "$UPSTREAM_URL" '
    "> /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'"
)


class ServiceConnectStack(Stack):
    """public ALB -> frontend (nginx) --Service Connect "http://api"--> api (whoami).

    - The cluster gets a default Cloud Map *HTTP namespace*
      (`<product>-<env>.local`, `CDK_SC_NAMESPACE`); Service Connect uses it
      to keep track of which tasks back which name.
    - `api` is a client-server Service Connect service: it publishes its
      named port mapping `http` as `api:80`.
    - `frontend` is a client-only Service Connect service: its tasks get an
      Envoy-based proxy that resolves `api` and balances requests across the
      api tasks - no load balancer in between, retries and outlier detection
      included, and per-request metrics in CloudWatch.
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

        self.vpc = build_vpc(self, config, "svc-connect")
        self.cluster = build_cluster(self, config, self.vpc, "svc-connect")
        namespace_name = env_str("CDK_SC_NAMESPACE", f"{config.product}-{config.environment}.local")
        # An HTTP namespace is all Service Connect needs (a private DNS one
        # would also create a Route 53 private hosted zone).
        self.namespace = self.cluster.add_default_cloud_map_namespace(
            name=namespace_name, type=servicediscovery.NamespaceType.HTTP, use_for_service_connect=True,
        )
        count = desired_count("CDK_SC_DESIRED_COUNT")
        # AWS recommends +256 CPU units and +64 MiB per task for the Service
        # Connect proxy container: 256/512 for the app -> 512/1024 per task.
        size: dict = {"cpu": 512, "memory": 1024}

        # --- api: client-server ------------------------------------------------
        self.api = fargate_service(
            self, config, self.cluster, construct_id="Api", purpose="sc-api",
            image=env_str("CDK_SC_API_IMAGE", API_IMAGE), container_port=80, desired=count, **size,
            environment={"WHOAMI_NAME": "api"},
            app_protocol=ecs.AppProtocol.http,  # type: ignore[arg-type]  # a jsii class property mypy reads as a method
            service_connect_configuration=ecs.ServiceConnectProps(
                namespace=self.namespace.namespace_arn,
                services=[
                    ecs.ServiceConnectService(
                        port_mapping_name="http", dns_name=API_DNS_NAME, port=80,
                        per_request_timeout=Duration.seconds(15),
                    )
                ],
                log_driver=log_driver(self, config, "sc-api-proxy", "ApiProxyLogGroup"),
                access_log_configuration=ecs.ServiceConnectAccessLogConfiguration(
                    format=ecs.ServiceConnectAccessLogFormat.JSON,
                ),
            ),
        )

        # --- frontend: client only, behind a public ALB ---------------------------
        self.frontend = fargate_service(
            self, config, self.cluster, construct_id="Frontend", purpose="sc-frontend",
            image=env_str("CDK_SC_FRONTEND_IMAGE", FRONTEND_IMAGE), container_port=80, desired=count, **size,
            environment={"UPSTREAM_URL": env_str("CDK_SC_UPSTREAM_URL", f"http://{API_DNS_NAME}:80")},
            entry_point=["sh", "-c"], command=[NGINX_START],
            health_check_grace_period=Duration.seconds(30),
            service_connect_configuration=ecs.ServiceConnectProps(namespace=self.namespace.namespace_arn),
        )
        # Service Connect needs the namespace before either service.
        self.api.node.add_dependency(self.namespace)
        self.frontend.node.add_dependency(self.namespace)
        # The frontend's proxy talks to the api tasks directly.
        self.api.connections.allow_from(self.frontend, ec2.Port.tcp(80), "Service Connect from the frontend")

        alb_name = bounded_name(config.product, config.environment, "sc", "alb", max_length=32)
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=alb_name, vpc=self.vpc, internet_facing=True,
        )
        apply_name_tag(self.alb, alb_name)
        self.alb.add_listener(
            "Http", port=listener_port("SERVICE_CONNECT"), protocol=elbv2.ApplicationProtocol.HTTP, open=True
        ).add_targets(
            "Frontend", port=80, targets=[self.frontend], deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(path="/health"),
        )

        cdk.CfnOutput(self, "Url", value=f"http://{self.alb.load_balancer_dns_name}:{listener_port('SERVICE_CONNECT')}/")
        cdk.CfnOutput(self, "NamespaceName", value=namespace_name)
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)


STACK_CLASS = ServiceConnectStack
