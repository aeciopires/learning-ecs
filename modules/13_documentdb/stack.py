"""Module 13 - Amazon DocumentDB: ECS tasks reading and writing documents over TLS.

AWS docs used while writing this module:
- aws_docdb DatabaseCluster / ClusterParameterGroup:
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_docdb/DatabaseCluster.html
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_docdb/ClusterParameterGroup.html
- Connecting programmatically (global-bundle.pem, mongosh flags, retryWrites=false):
  https://docs.aws.amazon.com/documentdb/latest/developerguide/connect_programmatically.html
- Encrypting data in transit (the `tls` cluster parameter):
  https://docs.aws.amazon.com/documentdb/latest/developerguide/security.encryption.ssl.html
- ECS container dependencies (dependsOn, condition SUCCESS) and bind mount volumes:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html#container_definition_dependson
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/bind-mounts.html
- Docker Hub - mongo (mongosh) and curlimages/curl: https://hub.docker.com/_/mongo  https://hub.docker.com/r/curlimages/curl

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import RemovalPolicy, SecretValue, Stack
from aws_cdk import aws_docdb as docdb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from shared.config import AppConfig, env_bool, env_int, env_str
from shared.database import generated_credentials
from shared.ecs import (
    build_cluster,
    capacity_provider_strategies,
    desired_count,
    execute_command_enabled,
    log_driver,
    runtime_platform,
)
from shared.naming import resource_name
from shared.network import build_vpc, data_subnets, task_subnets
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "DocumentDbStack"

MONGO_IMAGE = "mongo:8.0"
CURL_IMAGE = "curlimages/curl:8.22.0"
CA_BUNDLE_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
CA_BUNDLE_PATH = "/certs/global-bundle.pem"

# mongosh, connected with the flags DocumentDB documents (plus the admin
# authentication database), running the JavaScript in $JS.
MONGOSH = (
    'mongosh --quiet --host "$DOCDB_HOST:$DOCDB_PORT" --username "$DOCDB_USER" --password "$DOCDB_PASSWORD" '
    '--authenticationDatabase admin --retryWrites false $DOCDB_TLS --eval "$JS"'
)
WORKER_JS = (
    'const c = db.getSiblingDB("app").events; '
    'c.insertOne({task: process.env.HOSTNAME, at: new Date()}); '
    'print("task=" + process.env.HOSTNAME + " inserted; events=" + c.countDocuments({}));'
)
CLIENT_JS = (
    'const c = db.getSiblingDB("app").events; '
    'printjson({count: c.countDocuments({}), latest: c.find().sort({_id: -1}).limit(3).toArray()});'
)


class DocumentDbStack(Stack):
    """A DocumentDB cluster (CDK_DOCDB_INSTANCES instances) and two ECS consumers.

    - Instances are spread over the isolated subnets' AZs; with 2+, one is
      the primary and the rest are replicas DocumentDB fails over to.
    - TLS is on (DocumentDB's default). The official mongo image has no
      curl/wget, so each task first runs a `ca-bundle` container (the
      curlimages/curl image) that downloads Amazon's CA bundle into a task
      volume; the mongosh container depends on it finishing with SUCCESS.
    - `events` (a service) inserts a document and counts the collection
      every CDK_DOCDB_INTERVAL_SECONDS; `*-documentdb-client` (a one-off
      task definition) runs any JavaScript you pass in `JS`.
    - Bring your own cluster: CDK_DOCDB_ENDPOINT + CDK_DOCDB_SECRET_NAME
      (an existing secret with username/password) skip creating one - how
      this module runs on floci, whose CloudFormation doesn't create
      DocumentDB resources.
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

        purpose = "documentdb"
        tls = env_bool("CDK_DOCDB_TLS", True)
        port = env_int("CDK_DOCDB_PORT", 27017, minimum=1)
        existing_endpoint = env_str("CDK_DOCDB_ENDPOINT", "")
        self.vpc = build_vpc(self, config, purpose, isolated=True)
        self.cluster = build_cluster(self, config, self.vpc, purpose)
        self.db_security_group = ec2.SecurityGroup(
            self, "DocDbSecurityGroup", vpc=self.vpc, description="DocumentDB: only the ECS tasks",
            allow_all_outbound=False,
        )
        apply_name_tag(self.db_security_group, resource_name(config.product, config.environment, "sg", purpose))

        username = "docdbadmin"
        if existing_endpoint:
            self.database = None
            self.secret = secretsmanager.Secret.from_secret_name_v2(
                self, "ExistingSecret", env_str("CDK_DOCDB_SECRET_NAME", f"{config.product}-{config.environment}-secret-{purpose}")
            )
            host = existing_endpoint
        else:
            self.secret, secret_name = generated_credentials(self, config, purpose, username)
            parameter_group = None
            if not tls:
                family = env_str("CDK_DOCDB_PARAMETER_FAMILY", "docdb5.0")
                parameter_group = docdb.ClusterParameterGroup(
                    self, "NoTlsParameters", family=family, parameters={"tls": "disabled"},
                    description="TLS disabled - learning only",
                )
            cluster_name = resource_name(config.product, config.environment, "docdb")
            self.database = docdb.DatabaseCluster(
                self,
                "Database",
                db_cluster_name=cluster_name,
                engine_version=env_str("CDK_DOCDB_VERSION", "5.0.0"),
                master_user=docdb.Login(
                    username=username, password=SecretValue.secrets_manager(secret_name, json_field="password")
                ),
                instance_type=ec2.InstanceType(env_str("CDK_DOCDB_INSTANCE_TYPE", "t4g.medium")),
                instances=env_int("CDK_DOCDB_INSTANCES", 2, minimum=1),
                vpc=self.vpc,
                vpc_subnets=data_subnets(),
                security_group=self.db_security_group,
                port=port,
                parameter_group=parameter_group,
                storage_encrypted=True,
                removal_policy=RemovalPolicy.DESTROY,
                deletion_protection=False,
            )
            apply_name_tag(self.database, cluster_name)
            self.database.node.add_dependency(self.secret)
            host = self.database.cluster_endpoint.hostname

        self.task_environment = {
            "DOCDB_HOST": host,
            "DOCDB_PORT": str(port),
            "DOCDB_USER": username,
            "DOCDB_TLS": f"--tls --tlsCAFile {CA_BUNDLE_PATH}" if tls else "",
        }
        self.use_tls = tls

        # --- the worker service ----------------------------------------------------------
        worker_task = self._task_definition(
            config, "EventsTaskDefinition", f"{purpose}-events",
            command=f'while true; do {MONGOSH}; sleep "$INTERVAL"; done',
            extra_environment={"JS": WORKER_JS, "INTERVAL": str(env_int("CDK_DOCDB_INTERVAL_SECONDS", 15, minimum=1))},
        )
        service_name = resource_name(config.product, config.environment, purpose, "events")
        self.worker = ecs.FargateService(
            self, "EventsService", service_name=service_name, cluster=self.cluster, task_definition=worker_task,
            desired_count=desired_count("CDK_DOCDB_DESIRED_COUNT"), vpc_subnets=task_subnets(config),
            assign_public_ip=config.tasks_in_public_subnets, capacity_provider_strategies=capacity_provider_strategies(),
            min_healthy_percent=100, circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            enable_execute_command=execute_command_enabled(), propagate_tags=ecs.PropagatedTagSource.SERVICE,
        )
        apply_name_tag(self.worker, service_name)
        self._allow(self.worker.connections.security_groups[0], port, "events tasks")

        # --- the one-off client task -----------------------------------------------------
        self.client_task = self._task_definition(
            config, "ClientTaskDefinition", f"{purpose}-client", command=MONGOSH, extra_environment={"JS": CLIENT_JS},
        )
        self.client_security_group = ec2.SecurityGroup(
            self, "ClientSecurityGroup", vpc=self.vpc, description=f"{purpose} one-off client tasks"
        )
        self._allow(self.client_security_group, port, "one-off client task")

        cdk.CfnOutput(self, "Endpoint", value=host)
        cdk.CfnOutput(self, "ClientTaskDefinitionArn", value=self.client_task.task_definition_arn)
        cdk.CfnOutput(self, "ClientSecurityGroupId", value=self.client_security_group.security_group_id)
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)

    def _allow(self, peer_group: ec2.ISecurityGroup, port: int, description: str) -> None:
        self.db_security_group.add_ingress_rule(
            ec2.Peer.security_group_id(peer_group.security_group_id), ec2.Port.tcp(port), description
        )

    def _task_definition(self, config, construct_id, purpose, *, command, extra_environment) -> ecs.FargateTaskDefinition:
        family = resource_name(config.product, config.environment, purpose)
        # mongosh is a Node.js program: 256/512 is too tight for it.
        task = ecs.FargateTaskDefinition(
            self, construct_id, family=family, cpu=512, memory_limit_mib=1024, runtime_platform=runtime_platform(),
        )
        apply_name_tag(task, family)
        app = task.add_container(
            "mongosh", container_name="mongosh",
            image=ecs.ContainerImage.from_registry(env_str("CDK_DOCDB_CLIENT_IMAGE", MONGO_IMAGE)),
            entry_point=["sh", "-c"], command=[command],
            environment={**self.task_environment, **extra_environment},
            secrets={"DOCDB_PASSWORD": ecs.Secret.from_secrets_manager(self.secret, "password")},
            logging=log_driver(self, config, purpose, f"{construct_id}LogGroup"),
        )
        if self.use_tls:
            # A task-scoped volume (lives as long as the task) shared by the
            # two containers: curl writes the CA bundle, mongosh reads it.
            task.add_volume(name="certs")
            fetch = task.add_container(
                "ca-bundle", container_name="ca-bundle", essential=False,
                image=ecs.ContainerImage.from_registry(CURL_IMAGE),
                command=["-fsSL", "-o", CA_BUNDLE_PATH, CA_BUNDLE_URL],
                # The image runs as an unprivileged user by default; the
                # volume is owned by root, so write it as root.
                user="0",
                logging=log_driver(self, config, f"{purpose}-ca-bundle", f"{construct_id}CaLogGroup"),
            )
            fetch.add_mount_points(ecs.MountPoint(container_path="/certs", source_volume="certs", read_only=False))
            app.add_mount_points(ecs.MountPoint(container_path="/certs", source_volume="certs", read_only=True))
            app.add_container_dependencies(
                ecs.ContainerDependency(container=fetch, condition=ecs.ContainerDependencyCondition.SUCCESS)
            )
        return task


STACK_CLASS = DocumentDbStack
