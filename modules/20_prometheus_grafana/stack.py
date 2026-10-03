"""Module 20 - Prometheus and Grafana: an ADOT sidecar scrapes the app and remote-writes to Prometheus (self-hosted or AMP).

AWS and project docs used while writing this module:
- Set up metrics ingestion from Amazon ECS using AWS Distro for OpenTelemetry (AMP):
  https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-onboard-ingest-metrics-OpenTelemetry-ECS.html
- Use awscurl to query with Prometheus-compatible APIs (AMP):
  https://docs.aws.amazon.com/prometheus/latest/userguide/AMP-compatible-APIs.html
- ADOT collector configuration through AOT_CONFIG_CONTENT:
  https://aws-otel.github.io/docs/setup/ecs/config-through-ssm
- Prometheus configuration / remote write receiver / alerting rules:
  https://prometheus.io/docs/prometheus/latest/configuration/configuration/
  https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/
- Grafana provisioning: https://grafana.com/docs/grafana/latest/administration/provisioning/
- Caddy metrics: https://caddyserver.com/docs/metrics
- Docker Hub: caddy, amazon/aws-otel-collector, prom/prometheus, grafana/grafana

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import json
from typing import Any

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_aps as aps
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from shared.config import AppConfig, env_bool, env_str
from shared.ecs import build_cluster, desired_count, fargate_service, listener_port, log_driver
from shared.naming import bounded_name, resource_name
from shared.network import PRIVATE, PUBLIC, build_vpc
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "PrometheusGrafanaStack"

# Docker Hub images - re-check the tags on hub.docker.com.
CADDY_IMAGE = "caddy:2.11.4-alpine"
ADOT_IMAGE = "amazon/aws-otel-collector:v0.50.0"
PROMETHEUS_IMAGE = "prom/prometheus:v3.13.4"
GRAFANA_IMAGE = "grafana/grafana:13.0.10"

BACKENDS = ("self_hosted", "amp")
METRICS_PORT = 2019  # Caddy's admin API, which serves /metrics

# The application: Caddy answering /, /error (500) and /health, with Prometheus metrics on :2019/metrics.
CADDYFILE = """{
\tadmin 0.0.0.0:2019
\tmetrics
}
:80 {
\trespond /health "ok" 200
\trespond /error "simulated error" 500
\trespond "hello from {system.hostname}" 200
}
"""
# floci only (CDK_PROMETHEUS_SCRAPE_VIA_ALB): the metrics also on :80/metrics, see README.
CADDYFILE_ALB_METRICS = CADDYFILE.replace('\trespond /health', '\thandle /metrics {\n\t\tmetrics\n\t}\n\trespond /health')
CADDY_START = (
    "printf '%s' \"$CADDYFILE\" > /etc/caddy/Caddyfile && "
    "exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile"
)

# Prometheus alerting rules (evaluated by Prometheus; visible on its /alerts page).
RULES = {
    "groups": [{
        "name": "ecs-service",
        "rules": [
            {
                "alert": "HighErrorRate",
                "expr": 'sum(rate(caddy_http_request_duration_seconds_count{code=~"5.."}[5m]))'
                        " / sum(rate(caddy_http_request_duration_seconds_count[5m])) > 0.05",
                "for": "2m",
                "labels": {"severity": "page"},
                "annotations": {"summary": "More than 5% of the requests fail"},
            },
            {
                "alert": "TargetDown",
                "expr": 'up{job="caddy"} == 0',
                "for": "1m",
                "labels": {"severity": "page"},
                "annotations": {"summary": "A task's metrics endpoint stopped answering ({{ $labels.host_name }})"},
            },
        ],
    }],
}
PROMETHEUS_CONFIG = {
    "global": {"scrape_interval": "15s", "evaluation_interval": "15s"},
    "rule_files": ["/etc/prometheus/rules.yml"],
    "scrape_configs": [{"job_name": "prometheus", "static_configs": [{"targets": ["localhost:9090"]}]}],
}
# Config and rules come in as environment variables (JSON is valid YAML).
PROMETHEUS_START = (
    "printf '%s' \"$PROMETHEUS_CONFIG\" > /etc/prometheus/prometheus.yml && "
    "printf '%s' \"$PROMETHEUS_RULES\" > /etc/prometheus/rules.yml && "
    "exec /bin/prometheus --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus "
    "--storage.tsdb.retention.time=\"$RETENTION\" --web.enable-remote-write-receiver --web.enable-lifecycle"
)

# Grafana: a Prometheus data source and one dashboard, provisioned at start-up.
DASHBOARD: dict[str, Any] = {
    "uid": "learning-ecs-service",
    "title": "ECS service (Caddy)",
    "schemaVersion": 39,
    "time": {"from": "now-1h", "to": "now"},
    "refresh": "30s",
    "panels": [
        {"type": "timeseries", "title": "Requests/s by status code", "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
         "targets": [{"refId": "A", "legendFormat": "{{code}}",
                      "expr": "sum by (code) (rate(caddy_http_request_duration_seconds_count[1m]))"}]},
        {"type": "timeseries", "title": "Error ratio (5xx)", "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8},
         "fieldConfig": {"defaults": {"unit": "percentunit"}},
         "targets": [{"refId": "A", "legendFormat": "5xx",
                      "expr": 'sum(rate(caddy_http_request_duration_seconds_count{code=~"5.."}[5m]))'
                              " / sum(rate(caddy_http_request_duration_seconds_count[5m]))"}]},
        {"type": "timeseries", "title": "Latency p50 / p99", "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8},
         "fieldConfig": {"defaults": {"unit": "s"}},
         "targets": [
             {"refId": "A", "legendFormat": "p50", "expr": "histogram_quantile(0.5, sum by (le) "
                                                          "(rate(caddy_http_request_duration_seconds_bucket[5m])))"},
             {"refId": "B", "legendFormat": "p99", "expr": "histogram_quantile(0.99, sum by (le) "
                                                          "(rate(caddy_http_request_duration_seconds_bucket[5m])))"},
         ]},
        {"type": "stat", "title": "Tasks being scraped (up)", "gridPos": {"x": 12, "y": 8, "w": 6, "h": 8},
         "targets": [{"refId": "A", "expr": 'sum(up{job="caddy"})'}]},
        {"type": "timeseries", "title": "Requests/s per task", "gridPos": {"x": 18, "y": 8, "w": 6, "h": 8},
         "targets": [{"refId": "A", "legendFormat": "{{host_name}}",
                      "expr": "sum by (host_name) (rate(caddy_http_request_duration_seconds_count[1m]))"}]},
        {"type": "timeseries", "title": "Task memory (ADOT awsecscontainermetrics, MiB)",
         "gridPos": {"x": 0, "y": 16, "w": 12, "h": 8},
         "targets": [{"refId": "A", "legendFormat": "{{host_name}}",
                      "expr": '{__name__=~"ecs_task_memory_utilized.*"}'}]},
        {"type": "timeseries", "title": "Task CPU (ADOT awsecscontainermetrics)",
         "gridPos": {"x": 12, "y": 16, "w": 12, "h": 8},
         "targets": [{"refId": "A", "legendFormat": "{{host_name}}",
                      "expr": '{__name__=~"ecs_task_cpu_utilized.*"}'}]},
    ],
}
for _panel in DASHBOARD["panels"]:
    _panel["datasource"] = {"type": "prometheus", "uid": "prometheus"}
    for _target in _panel["targets"]:
        _target["datasource"] = {"type": "prometheus", "uid": "prometheus"}

GRAFANA_START = r"""set -e
P="$GF_PATHS_PROVISIONING"
mkdir -p "$P/datasources" "$P/dashboards" "$P/plugins" "$P/alerting" /tmp/dashboards
printf 'apiVersion: 1\ndatasources:\n  - name: Prometheus\n    type: prometheus\n    uid: prometheus\n    access: proxy\n    url: %s\n    isDefault: true\n' "$PROMETHEUS_URL" > "$P/datasources/prometheus.yaml"
printf 'apiVersion: 1\nproviders:\n  - name: learning-ecs\n    type: file\n    options:\n      path: /tmp/dashboards\n' > "$P/dashboards/learning-ecs.yaml"
printf '%s' "$DASHBOARD_JSON" > /tmp/dashboards/service.json
exec /run.sh"""


def adot_config(remote_write_url: str, *, region: str | None, ecs_metrics: bool,
                scrape_target: str = f"localhost:{METRICS_PORT}") -> dict:
    """The ADOT collector configuration (AOT_CONFIG_CONTENT), as a dict.

    - prometheus receiver: scrapes the app container on localhost:2019.
    - awsecscontainermetrics receiver (optional): task CPU/memory/network
      from the ECS task metadata endpoint.
    - resource_detection: adds the task (ecs detector, real AWS) and the
      host name (system detector) to every series, so tasks don't collide.
    - prometheus_remote_write exporter: to Prometheus, signed with SigV4
      when `region` is given (Amazon Managed Service for Prometheus).
    """
    exporter: dict = {"endpoint": remote_write_url, "resource_to_telemetry_conversion": {"enabled": True}}
    extensions: dict = {"health_check": {}}
    if region:
        exporter["auth"] = {"authenticator": "sigv4auth"}
        extensions["sigv4auth"] = {"region": region, "service": "aps"}
    receivers: dict = {
        "prometheus": {"config": {"scrape_configs": [{
            "job_name": "caddy", "scrape_interval": "15s",
            "static_configs": [{"targets": [scrape_target]}],
        }]}},
    }
    pipelines: dict = {
        "metrics": {"receivers": ["prometheus"], "processors": ["resource_detection", "batch"],
                    "exporters": ["prometheus_remote_write"]},
    }
    processors: dict = {
        "resource_detection": {"detectors": ["ecs", "system"], "system": {"hostname_sources": ["os"]}},
        "batch": {},
    }
    if ecs_metrics:
        receivers["awsecscontainermetrics"] = {"collection_interval": "20s"}
        processors["filter"] = {"metrics": {"include": {"match_type": "strict", "metric_names": [
            "ecs.task.memory.utilized", "ecs.task.memory.reserved", "ecs.task.cpu.utilized",
            "ecs.task.cpu.reserved", "ecs.task.network.rate.rx", "ecs.task.network.rate.tx",
        ]}}}
        pipelines["metrics/ecs"] = {"receivers": ["awsecscontainermetrics"],
                                    "processors": ["filter", "resource_detection", "batch"],
                                    "exporters": ["prometheus_remote_write"]}
    return {
        "receivers": receivers,
        "processors": processors,
        "exporters": {"prometheus_remote_write": exporter},
        "extensions": extensions,
        "service": {"extensions": list(extensions), "pipelines": pipelines},
    }


class PrometheusGrafanaStack(Stack):
    """An app whose metrics flow to Prometheus through an ADOT sidecar.

    CDK_PROMETHEUS_BACKEND=self_hosted (default): Prometheus and Grafana run
    as ECS services; the sidecars remote-write to Prometheus through an
    internal ALB; a public ALB serves the app, the Prometheus UI and
    Grafana (the last two only to CDK_MONITORING_ALLOWED_CIDR).
    CDK_PROMETHEUS_BACKEND=amp: an Amazon Managed Service for Prometheus
    workspace instead, written to with SigV4 by the same sidecars.
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

        backend = env_str("CDK_PROMETHEUS_BACKEND", "self_hosted").lower()
        if backend not in BACKENDS:
            raise ValueError(f"CDK_PROMETHEUS_BACKEND={backend!r}: must be one of {', '.join(BACKENDS)}")
        purpose = "prom"
        self.vpc = build_vpc(self, config, purpose)
        self.cluster = build_cluster(self, config, self.vpc, purpose)
        allowed_cidr = env_str("CDK_MONITORING_ALLOWED_CIDR", self.vpc.vpc_cidr_block)

        # --- public ALB (the app; Prometheus/Grafana UIs when self-hosted) -------------------------------
        alb_name = bounded_name(config.product, config.environment, "alb", purpose, max_length=32)
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=alb_name, vpc=self.vpc, internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=PUBLIC),
        )
        apply_name_tag(self.alb, alb_name)

        # --- where the metrics go -------------------------------------------------------------------
        self.workspace: aps.CfnWorkspace | None = None
        self.internal_alb: elbv2.ApplicationLoadBalancer | None = None
        if backend == "amp":
            self.workspace = aps.CfnWorkspace(
                self, "Workspace", alias=resource_name(config.product, config.environment, "amp", purpose),
            )
            remote_write_url = f"{self.workspace.attr_prometheus_endpoint}api/v1/remote_write"
            adot_region: str | None = self.region
        else:
            remote_write_url = self._self_hosted(config, purpose, allowed_cidr)
            adot_region = None

        # --- the application: Caddy + ADOT sidecar ----------------------------------------------------
        app_port = listener_port("PROMETHEUS_APP")
        # floci runs a task's containers without a shared localhost: there, the
        # sidecar scrapes through the ALB instead (with one task - see README).
        via_alb = env_bool("CDK_PROMETHEUS_SCRAPE_VIA_ALB", False)
        scrape_target = f"{self.alb.load_balancer_dns_name}:{app_port}" if via_alb else f"localhost:{METRICS_PORT}"
        self.app = fargate_service(
            self, config, self.cluster, construct_id="Web", purpose=f"{purpose}-web", image=CADDY_IMAGE,
            container_port=80, desired=desired_count("CDK_PROMETHEUS_DESIRED_COUNT"), cpu=512, memory=1024,
            environment={"CADDYFILE": CADDYFILE_ALB_METRICS if via_alb else CADDYFILE},
            entry_point=["sh", "-c"], command=[CADDY_START],
            health_check_grace_period=Duration.seconds(30),
        )
        ecs_metrics = env_bool("CDK_PROMETHEUS_ECS_METRICS", True)
        self.app.task_definition.add_container(
            "AdotCollector", container_name="adot-collector",
            image=ecs.ContainerImage.from_registry(env_str("CDK_PROMETHEUS_ADOT_IMAGE", ADOT_IMAGE)),
            essential=True, memory_reservation_mib=128,
            environment={"AOT_CONFIG_CONTENT": json.dumps(
                adot_config(remote_write_url, region=adot_region, ecs_metrics=ecs_metrics,
                            scrape_target=scrape_target))},
            logging=log_driver(self, config, f"{purpose}-web-adot", "AdotLogGroup"),
        )
        if self.workspace is not None:
            self.app.task_definition.task_role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonPrometheusRemoteWriteAccess")
            )
        app_listener = self.alb.add_listener(
            "App", port=app_port, protocol=elbv2.ApplicationProtocol.HTTP, open=True,
        )
        app_listener.add_targets(
            "Web", target_group_name=bounded_name(config.product, config.environment, "tg", purpose, "web",
                                                  max_length=32),
            port=80, protocol=elbv2.ApplicationProtocol.HTTP, targets=[self.app],
            health_check=elbv2.HealthCheck(path="/health", healthy_http_codes="200", interval=Duration.seconds(15)),
            deregistration_delay=Duration.seconds(30),
        )

        cdk.CfnOutput(self, "Backend", value=backend)
        cdk.CfnOutput(self, "AppUrl", value=f"http://{self.alb.load_balancer_dns_name}:{app_port}/")
        cdk.CfnOutput(self, "RemoteWriteUrl", value=remote_write_url)
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        if self.workspace is not None:
            cdk.CfnOutput(self, "WorkspaceId", value=self.workspace.attr_workspace_id)
            cdk.CfnOutput(self, "QueryUrl", value=f"{self.workspace.attr_prometheus_endpoint}api/v1/query")

    def _self_hosted(self, config: AppConfig, purpose: str, allowed_cidr: str) -> str:
        """Prometheus + Grafana services; returns the remote-write URL."""
        # Internal ALB: the sidecars write here, Grafana reads here.
        internal_name = bounded_name(config.product, config.environment, "alb", purpose, "int", max_length=32)
        self.internal_alb = elbv2.ApplicationLoadBalancer(
            self, "InternalAlb", load_balancer_name=internal_name, vpc=self.vpc, internet_facing=False,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=PRIVATE if config.nat_gateways else PUBLIC),
        )
        apply_name_tag(self.internal_alb, internal_name)
        internal_port = listener_port("PROMETHEUS_INTERNAL", 9090)
        internal_listener = self.internal_alb.add_listener(
            "Prometheus", port=internal_port, protocol=elbv2.ApplicationProtocol.HTTP, open=False,
        )
        self.internal_alb.connections.allow_from(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block), ec2.Port.tcp(internal_port), "Prometheus from inside the VPC"
        )
        internal_url = f"http://{self.internal_alb.load_balancer_dns_name}:{internal_port}"

        # Prometheus: one task. Its TSDB lives on the task's ephemeral storage (lost on replacement).
        self.prometheus = fargate_service(
            self, config, self.cluster, construct_id="Prometheus", purpose=f"{purpose}-server",
            image=env_str("CDK_PROMETHEUS_IMAGE", PROMETHEUS_IMAGE), container_port=9090, desired=1,
            cpu=512, memory=1024, min_healthy_percent=0,
            environment={"PROMETHEUS_CONFIG": json.dumps(PROMETHEUS_CONFIG), "PROMETHEUS_RULES": json.dumps(RULES),
                         "RETENTION": env_str("CDK_PROMETHEUS_RETENTION", "1d")},
            entry_point=["sh", "-c"], command=[PROMETHEUS_START],
            health_check_grace_period=Duration.seconds(60),
        )
        prometheus_health = elbv2.HealthCheck(path="/-/healthy", healthy_http_codes="200", interval=Duration.seconds(15))
        internal_listener.add_targets(
            "Prometheus", target_group_name=bounded_name(config.product, config.environment, "tg", purpose, "int",
                                                         max_length=32),
            port=9090, protocol=elbv2.ApplicationProtocol.HTTP, targets=[self.prometheus],
            health_check=prometheus_health, deregistration_delay=Duration.seconds(10),
        )

        # Grafana: one task, admin password from Secrets Manager.
        secret_name = resource_name(config.product, config.environment, "secret", "grafana")
        self.grafana_secret = secretsmanager.Secret(
            self, "GrafanaAdminSecret", secret_name=secret_name, description="Grafana admin user",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "admin"}), generate_string_key="password",
                exclude_punctuation=True, password_length=24,
            ),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        apply_name_tag(self.grafana_secret, secret_name)
        self.grafana = fargate_service(
            self, config, self.cluster, construct_id="Grafana", purpose=f"{purpose}-grafana",
            image=env_str("CDK_GRAFANA_IMAGE", GRAFANA_IMAGE), container_port=3000, desired=1,
            cpu=512, memory=1024, min_healthy_percent=0,
            environment={"GF_PATHS_PROVISIONING": "/tmp/provisioning", "PROMETHEUS_URL": internal_url,
                         "DASHBOARD_JSON": json.dumps(DASHBOARD), "START": GRAFANA_START},
            secrets={"GF_SECURITY_ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(self.grafana_secret, "password")},
            entry_point=["sh", "-c"], command=['eval "$START"'],
            health_check_grace_period=Duration.seconds(60),
        )

        # UIs on the public ALB, only for CDK_MONITORING_ALLOWED_CIDR.
        for name, service, port, container_port, health in (
            # Ports of their own: the app has 80 on the same load balancer.
            ("PrometheusUi", self.prometheus, listener_port("PROMETHEUS", 9090), 9090, prometheus_health),
            ("GrafanaUi", self.grafana, listener_port("GRAFANA", 3000), 3000,
             elbv2.HealthCheck(path="/api/health", healthy_http_codes="200", interval=Duration.seconds(15))),
        ):
            listener = self.alb.add_listener(name, port=port, protocol=elbv2.ApplicationProtocol.HTTP, open=False)
            self.alb.connections.allow_from(ec2.Peer.ipv4(allowed_cidr), ec2.Port.tcp(port), f"{name} viewers")
            listener.add_targets(
                name, target_group_name=bounded_name(config.product, config.environment, "tg", purpose, name.lower(),
                                                     max_length=32),
                port=container_port, protocol=elbv2.ApplicationProtocol.HTTP, targets=[service],
                health_check=health, deregistration_delay=Duration.seconds(10),
            )
            cdk.CfnOutput(self, f"{name}Url", value=f"http://{self.alb.load_balancer_dns_name}:{port}/")
        cdk.CfnOutput(self, "GrafanaSecretName", value=secret_name)
        return f"{internal_url}/api/v1/write"


STACK_CLASS = PrometheusGrafanaStack
