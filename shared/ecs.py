"""Small ECS building blocks every module reuses, so each stays short.

- `build_cluster()`   - an ECS cluster with Fargate capacity providers and
                        Container Insights (the setting comes from .env).
- `log_driver()`      - the `awslogs` driver writing to a log group with a
                        fixed name and the retention set in .env.

References:
- https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/Cluster.html
- https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/LogDrivers.html
- https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_awslogs.html
- https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/ContainerInsights.html
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_logs as logs
from constructs import Construct

from shared.config import LOG_RETENTION_DAYS, AppConfig, env_bool, env_int, env_str
from shared.naming import resource_name
from shared.network import task_subnets
from shared.tagging import apply_name_tag

# aws_logs.RetentionDays lists the same values as LOG_RETENTION_DAYS, in the
# same order, followed by INFINITE.
_RETENTION = dict(zip(LOG_RETENTION_DAYS, list(logs.RetentionDays)[: len(LOG_RETENTION_DAYS)], strict=True))

CONTAINER_INSIGHTS = {
    "disabled": ecs.ContainerInsights.DISABLED,
    "enabled": ecs.ContainerInsights.ENABLED,
    "enhanced": ecs.ContainerInsights.ENHANCED,
}


def retention(config: AppConfig) -> logs.RetentionDays:
    return _RETENTION[config.log_retention_days]


def container_insights() -> ecs.ContainerInsights:
    """CDK_CONTAINER_INSIGHTS: disabled | enabled | enhanced (default enhanced)."""
    value = env_str("CDK_CONTAINER_INSIGHTS", "enhanced").lower()
    if value not in CONTAINER_INSIGHTS:
        raise ValueError(f"CDK_CONTAINER_INSIGHTS={value!r}: must be one of {', '.join(CONTAINER_INSIGHTS)}")
    return CONTAINER_INSIGHTS[value]


def build_cluster(scope: Construct, config: AppConfig, vpc: ec2.IVpc, purpose: str) -> ecs.Cluster:
    """A cluster with the FARGATE and FARGATE_SPOT capacity providers enabled."""
    cluster_name = resource_name(config.product, config.environment, "ecs", purpose)
    cluster = ecs.Cluster(
        scope,
        "Cluster",
        cluster_name=cluster_name,
        vpc=vpc,
        enable_fargate_capacity_providers=True,
        container_insights_v2=container_insights(),
    )
    apply_name_tag(cluster, cluster_name)
    return cluster


def log_driver(scope: Construct, config: AppConfig, purpose: str, construct_id: str = "LogGroup") -> ecs.LogDriver:
    """`awslogs` to /ecs/<product>/<env>/<purpose>, removed with the stack."""
    log_group_name = f"/ecs/{config.product}/{config.environment}/{purpose}"
    log_group = logs.LogGroup(
        scope,
        construct_id,
        log_group_name=log_group_name,
        retention=retention(config),
        removal_policy=cdk.RemovalPolicy.DESTROY,
    )
    apply_name_tag(log_group, log_group_name)
    return ecs.LogDrivers.aws_logs(stream_prefix=purpose, log_group=log_group)



# --- service-wide knobs (every module's services read these) -----------------

CPU_ARCHITECTURES = {"x86_64": ecs.CpuArchitecture.X86_64, "arm64": ecs.CpuArchitecture.ARM64}


def desired_count(variable: str | None = None, default: int = 2) -> int:
    """How many tasks a service keeps running.

    `variable` (a module's own knob, e.g. CDK_ALB_DESIRED_COUNT) wins over
    CDK_DESIRED_COUNT, which wins over `default`. The default of 2 puts one
    task in each of the two default Availability Zones.
    """
    fallback = env_int("CDK_DESIRED_COUNT", default, minimum=0)
    return env_int(variable, fallback, minimum=0) if variable else fallback


def capacity_provider_strategies() -> list[ecs.CapacityProviderStrategy]:
    """FARGATE always runs the first task (base=1); CDK_FARGATE_SPOT_WEIGHT
    sets how many FARGATE_SPOT tasks run for each further FARGATE one (0 =
    no Spot). See https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-capacity-providers.html
    """
    spot_weight = env_int("CDK_FARGATE_SPOT_WEIGHT", 0, minimum=0)
    strategies = [ecs.CapacityProviderStrategy(capacity_provider="FARGATE", base=1, weight=1)]
    if spot_weight:
        strategies.append(ecs.CapacityProviderStrategy(capacity_provider="FARGATE_SPOT", weight=spot_weight))
    return strategies


def runtime_platform() -> ecs.RuntimePlatform:
    """CDK_CPU_ARCHITECTURE: x86_64 (default) or arm64 (AWS Graviton)."""
    value = env_str("CDK_CPU_ARCHITECTURE", "x86_64").lower()
    if value not in CPU_ARCHITECTURES:
        raise ValueError(f"CDK_CPU_ARCHITECTURE={value!r}: must be one of {', '.join(CPU_ARCHITECTURES)}")
    return ecs.RuntimePlatform(
        cpu_architecture=CPU_ARCHITECTURES[value],
        operating_system_family=ecs.OperatingSystemFamily.LINUX,
    )


def execute_command_enabled() -> bool:
    """CDK_ENABLE_EXECUTE_COMMAND (default true): allow `aws ecs execute-command`."""
    return env_bool("CDK_ENABLE_EXECUTE_COMMAND", True)


def listener_port(name: str, default: int = 80) -> int:
    """CDK_PORT_<NAME>, or `default` - see .env.example ("Listener ports") for why floci needs one per listener."""
    return env_int(f"CDK_PORT_{name}", default, minimum=1)


def fargate_service(
    scope: Construct,
    config: AppConfig,
    cluster: ecs.ICluster,
    *,
    construct_id: str,
    purpose: str,
    image: str,
    container_port: int | None = None,
    desired: int = 2,
    cpu: int = 256,
    memory: int = 512,
    environment: dict[str, str] | None = None,
    secrets: dict[str, ecs.Secret] | None = None,
    command: list[str] | None = None,
    entry_point: list[str] | None = None,
    health_check: ecs.HealthCheck | None = None,
    security_groups: list[ec2.ISecurityGroup] | None = None,
    port_name: str = "http",
    app_protocol: ecs.AppProtocol | None = None,
    min_healthy_percent: int = 100,
    circuit_breaker: bool = True,
    container_options: dict | None = None,
    logging: ecs.LogDriver | None = None,
    **service_kwargs,
) -> ecs.FargateService:
    """One Fargate service with one container - module 02's pattern in a function.

    Names follow shared/naming.py (`<product>-<env>-<purpose>` for the task
    family and the service), logs go to `/ecs/<product>/<env>/<purpose>`, and
    the service-wide knobs (Spot weight, architecture, ECS Exec) come from
    .env. Anything else `FargateService` accepts can be passed through
    `service_kwargs` (load balancer settings, Service Connect, ...), and
    anything else `add_container` accepts through `container_options`
    (restart policy, ...).
    Module 02 shows every one of these pieces written out by hand.
    """
    family = resource_name(config.product, config.environment, purpose)
    task_definition = ecs.FargateTaskDefinition(
        scope,
        f"{construct_id}TaskDefinition",
        family=family,
        cpu=cpu,
        memory_limit_mib=memory,
        runtime_platform=runtime_platform(),
    )
    apply_name_tag(task_definition, family)
    port_mappings = (
        [ecs.PortMapping(container_port=container_port, name=port_name, app_protocol=app_protocol)]
        if container_port
        else None
    )
    task_definition.add_container(
        "app",
        container_name="app",
        image=ecs.ContainerImage.from_registry(image),
        port_mappings=port_mappings,
        # awslogs to /ecs/<product>/<env>/<purpose>, unless the caller routes logs elsewhere (FireLens).
        logging=logging or log_driver(scope, config, purpose, f"{construct_id}LogGroup"),
        environment=environment,
        secrets=secrets,
        command=command,
        entry_point=entry_point,
        health_check=health_check,
        **(container_options or {}),
    )
    service = ecs.FargateService(
        scope,
        f"{construct_id}Service",
        service_name=family,
        cluster=cluster,
        task_definition=task_definition,
        desired_count=desired,
        vpc_subnets=task_subnets(config),
        assign_public_ip=config.tasks_in_public_subnets,
        security_groups=security_groups,
        capacity_provider_strategies=capacity_provider_strategies(),
        min_healthy_percent=min_healthy_percent,
        # The circuit breaker only applies to rolling updates (module 17).
        circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True) if circuit_breaker else None,
        enable_execute_command=execute_command_enabled(),
        availability_zone_rebalancing=ecs.AvailabilityZoneRebalancing.ENABLED,
        propagate_tags=ecs.PropagatedTagSource.SERVICE,
        **service_kwargs,
    )
    apply_name_tag(service, family)
    return service

