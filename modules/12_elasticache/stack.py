"""Module 12 - ElastiCache (Valkey): ECS tasks reading and writing a Multi-AZ cache.

AWS docs used while writing this module:
- CfnReplicationGroup / CfnSubnetGroup (no stable L2 exists for ElastiCache):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_elasticache/CfnReplicationGroup.html
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_elasticache/CfnSubnetGroup.html
- AWS::ElastiCache::ReplicationGroup (properties and return values):
  https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-elasticache-replicationgroup.html
- Minimizing downtime with Multi-AZ (automatic failover):
  https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/AutoFailover.html
- ElastiCache in-transit encryption (TLS): https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/in-transit-encryption.html
- Finding connection endpoints (primary / reader):
  https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Endpoints.html
- Docker Hub - valkey/valkey (valkey-cli): https://hub.docker.com/r/valkey/valkey

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticache as elasticache
from constructs import Construct

from shared.config import AppConfig, env_bool, env_int, env_str
from shared.ecs import build_cluster, desired_count, fargate_service, log_driver, runtime_platform
from shared.naming import bounded_name, resource_name
from shared.network import build_vpc, data_subnets
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "ElastiCacheStack"

ENGINES = ("valkey", "redis")
VALKEY_IMAGE = "valkey/valkey:8.1-alpine"
# The CA bundle shipped in the image: verifies ElastiCache's TLS certificate.
TLS_ARGS = "--tls --cacert /etc/ssl/certs/ca-certificates.crt"

# The worker: increments a shared counter on the primary, reads it back from
# the reader endpoint, prints both - every $INTERVAL seconds.
WORKER_COMMAND = (
    'while true; do '
    'n=$(valkey-cli $CACHE_TLS -h "$CACHE_HOST" -p "$CACHE_PORT" INCR visits); '
    'r=$(valkey-cli $CACHE_TLS -h "$READER_HOST" -p "$CACHE_PORT" GET visits); '
    'echo "task=$HOSTNAME wrote visits=$n read-from-replica visits=$r"; '
    'sleep "$INTERVAL"; done'
)
# The one-off client: any command in $CMD (default: replication status).
CLIENT_COMMAND = 'valkey-cli $CACHE_TLS -h "$CACHE_HOST" -p "$CACHE_PORT" $CMD'


class ElastiCacheStack(Stack):
    """A Valkey replication group (primary + CDK_CACHE_REPLICAS replicas) and two ECS consumers.

    - With replicas, the group is Multi-AZ with automatic failover: if the
      primary fails, a replica is promoted and the *primary endpoint* moves
      to it. Reads can go to the *reader endpoint* (all replicas).
    - Encryption in transit (TLS) and at rest are on; only the two task
      security groups may connect on the cache port.
    - `counter` (a service) and `*-elasticache-client` (a one-off task
      definition) both use the official valkey image's `valkey-cli`.
    - Bring your own cache: with CDK_CACHE_ENDPOINT set, no replication
      group is created and the tasks use that endpoint instead. That's how
      this module runs on floci, whose CloudFormation doesn't create
      ElastiCache resources (create the cache with the AWS CLI there - see
      the README).
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

        engine = env_str("CDK_CACHE_ENGINE", "valkey").lower()
        if engine not in ENGINES:
            raise ValueError(f"CDK_CACHE_ENGINE={engine!r}: must be one of {', '.join(ENGINES)}")
        tls = env_bool("CDK_CACHE_TLS", True)
        port = env_int("CDK_CACHE_PORT", 6379, minimum=1)
        existing_endpoint = env_str("CDK_CACHE_ENDPOINT", "")

        purpose = "elasticache"
        self.vpc = build_vpc(self, config, purpose, isolated=True)
        self.cluster = build_cluster(self, config, self.vpc, purpose)
        self.cache_security_group = ec2.SecurityGroup(
            self, "CacheSecurityGroup", vpc=self.vpc, description=f"{engine} cache: only the ECS tasks",
            allow_all_outbound=False,
        )
        apply_name_tag(self.cache_security_group, resource_name(config.product, config.environment, "sg", purpose))

        if existing_endpoint:
            self.replication_group = None
            primary_host = reader_host = existing_endpoint
        else:
            subnet_group_name = resource_name(config.product, config.environment, "cache-subnet")
            self.subnet_group = elasticache.CfnSubnetGroup(
                self, "CacheSubnetGroup", cache_subnet_group_name=subnet_group_name,
                description=f"{config.product} {config.environment} cache subnets (isolated tier)",
                subnet_ids=self.vpc.select_subnets(subnet_group_name=data_subnets().subnet_group_name).subnet_ids,
            )
            apply_name_tag(self.subnet_group, subnet_group_name)
            replicas = env_int("CDK_CACHE_REPLICAS", 1, minimum=0)
            group_id = bounded_name(config.product, config.environment, engine, max_length=40)
            self.replication_group = elasticache.CfnReplicationGroup(
                self,
                "ReplicationGroup",
                replication_group_id=group_id,
                replication_group_description=f"{config.product} {config.environment} {engine} cache",
                engine=engine,
                engine_version=env_str("CDK_CACHE_ENGINE_VERSION", "8.1" if engine == "valkey" else "7.1"),
                cache_node_type=env_str("CDK_CACHE_NODE_TYPE", "cache.t4g.micro"),
                num_cache_clusters=1 + replicas,
                # Both need at least one replica.
                automatic_failover_enabled=replicas > 0,
                multi_az_enabled=replicas > 0,
                port=port,
                cache_subnet_group_name=self.subnet_group.ref,
                security_group_ids=[self.cache_security_group.security_group_id],
                transit_encryption_enabled=tls,
                at_rest_encryption_enabled=True,
                snapshot_retention_limit=env_int("CDK_CACHE_SNAPSHOT_DAYS", 1, minimum=0),
            )
            self.replication_group.add_resource_dependency(self.subnet_group)
            apply_name_tag(self.replication_group, group_id)
            primary_host = self.replication_group.attr_primary_end_point_address
            reader_host = (
                self.replication_group.attr_reader_end_point_address if replicas > 0 else primary_host
            )

        environment = {
            "CACHE_HOST": primary_host,
            "READER_HOST": reader_host,
            "CACHE_PORT": str(port),
            "CACHE_TLS": TLS_ARGS if tls else "",
        }
        image = env_str("CDK_CACHE_CLIENT_IMAGE", VALKEY_IMAGE)

        # --- the worker service (no load balancer: it only talks to the cache) ----
        self.worker = fargate_service(
            self, config, self.cluster, construct_id="Counter", purpose=f"{purpose}-counter", image=image,
            desired=desired_count("CDK_CACHE_DESIRED_COUNT"), entry_point=["sh", "-c"], command=[WORKER_COMMAND],
            environment={**environment, "INTERVAL": str(env_int("CDK_CACHE_INTERVAL_SECONDS", 10, minimum=1))},
        )
        self.cache_security_group.add_ingress_rule(
            ec2.Peer.security_group_id(self.worker.connections.security_groups[0].security_group_id),
            ec2.Port.tcp(port), "counter tasks",
        )

        # --- the one-off client task --------------------------------------------------
        client_family = resource_name(config.product, config.environment, purpose, "client")
        self.client_task = ecs.FargateTaskDefinition(
            self, "ClientTaskDefinition", family=client_family, cpu=256, memory_limit_mib=512,
            runtime_platform=runtime_platform(),
        )
        apply_name_tag(self.client_task, client_family)
        self.client_task.add_container(
            "client", container_name="client", image=ecs.ContainerImage.from_registry(image),
            entry_point=["sh", "-c"], command=[CLIENT_COMMAND],
            environment={**environment, "CMD": "INFO replication"},
            logging=log_driver(self, config, f"{purpose}-client", "ClientLogGroup"),
        )
        self.client_security_group = ec2.SecurityGroup(
            self, "ClientSecurityGroup", vpc=self.vpc, description=f"{client_family} one-off tasks"
        )
        self.cache_security_group.add_ingress_rule(
            ec2.Peer.security_group_id(self.client_security_group.security_group_id),
            ec2.Port.tcp(port), "one-off client task",
        )

        cdk.CfnOutput(self, "PrimaryEndpoint", value=primary_host)
        cdk.CfnOutput(self, "ReaderEndpoint", value=reader_host)
        cdk.CfnOutput(self, "ClientTaskDefinitionArn", value=self.client_task.task_definition_arn)
        cdk.CfnOutput(self, "ClientSecurityGroupId", value=self.client_security_group.security_group_id)
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)


STACK_CLASS = ElastiCacheStack
