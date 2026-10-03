"""Module 15 - S3: a scheduled ECS task writes to a bucket, another task reads it (task roles).

AWS docs used while writing this module:
- aws_s3 Bucket (encryption, block public access, enforce SSL, versioning, lifecycle):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_s3/Bucket.html
- aws_scheduler Schedule / aws_scheduler_targets EcsRunFargateTask:
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_scheduler/Schedule.html
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_scheduler_targets/EcsRunFargateTask.html
- Amazon ECS scheduled tasks (EventBridge Scheduler):
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/tasks-scheduled-eventbridge-scheduler.html
- Task IAM role: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html
- Docker Hub - amazon/aws-cli: https://hub.docker.com/r/amazon/aws-cli

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from constructs import Construct

from shared.config import AppConfig, env_bool, env_int, env_str
from shared.ecs import build_cluster, capacity_provider_strategies, log_driver, runtime_platform
from shared.naming import resource_name
from shared.network import build_vpc, task_subnets
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "S3Stack"

AWS_CLI_IMAGE = "amazon/aws-cli:2.37.8"

# Writes a small JSON report: when, which task, how many objects are already there.
WRITER_COMMAND = (
    'now=$(date -u +%Y-%m-%dT%H-%M-%SZ); '
    'count=$(aws s3 ls "s3://$BUCKET/reports/" | wc -l); '
    'printf \'{"generated_at":"%s","task":"%s","previous_reports":%s}\\n\' "$now" "$HOSTNAME" "$count" '
    '| aws s3 cp - "s3://$BUCKET/reports/$now.json" --content-type application/json && echo "wrote reports/$now.json"'
)
# Lists the newest reports and prints the latest one.
READER_COMMAND = (
    'aws s3 ls "s3://$BUCKET/reports/" | tail -5; '
    'latest=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix reports/ '
    '--query "sort_by(Contents,&LastModified)[-1].Key" --output text); '
    'echo "latest: $latest"; aws s3 cp "s3://$BUCKET/$latest" -'
)


class S3Stack(Stack):
    """A private bucket, a scheduled writer task and an on-demand reader task.

    - The bucket blocks public access, enforces TLS, encrypts with S3-managed
      keys, (optionally) keeps versions, and expires reports after
      CDK_S3_EXPIRE_DAYS days.
    - The writer runs on a schedule (EventBridge Scheduler -> ECS RunTask);
      its task role may only PutObject under `reports/` and list the
      bucket. The reader's role may only read.
    - Neither container has credentials of its own: the AWS CLI picks up
      the task role from the container credentials endpoint ECS provides.
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

        purpose = "s3"
        self.vpc = build_vpc(self, config, purpose)
        self.cluster = build_cluster(self, config, self.vpc, purpose)

        # Bucket names are global: account and region keep this one unique.
        # Fn.join, not resource_name(): account/region may be unresolved tokens.
        bucket_name = cdk.Fn.join(
            "-", [resource_name(config.product, config.environment, "reports"), self.account, self.region]
        )
        self.bucket = s3.Bucket(
            self,
            "ReportsBucket",
            bucket_name=bucket_name,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=env_bool("CDK_S3_VERSIONED", False),
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-reports", prefix="reports/",
                    expiration=Duration.days(env_int("CDK_S3_EXPIRE_DAYS", 30, minimum=1)),
                    noncurrent_version_expiration=Duration.days(7),
                )
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        apply_name_tag(self.bucket, resource_name(config.product, config.environment, "reports"))
        image = env_str("CDK_S3_IMAGE", AWS_CLI_IMAGE)

        self.writer_task = self._task(config, "WriterTaskDefinition", f"{purpose}-writer", image, WRITER_COMMAND)
        self.bucket.grant_put(self.writer_task.task_role, "reports/*")
        self.bucket.grant_read(self.writer_task.task_role)  # ListBucket for the report count
        self.reader_task = self._task(config, "ReaderTaskDefinition", f"{purpose}-reader", image, READER_COMMAND)
        self.bucket.grant_read(self.reader_task.task_role)

        self.task_security_group = ec2.SecurityGroup(
            self, "TaskSecurityGroup", vpc=self.vpc, description="s3 writer/reader tasks (egress only)"
        )

        # --- the schedule ------------------------------------------------------------------
        schedule_name = resource_name(config.product, config.environment, purpose, "writer")
        self.schedule = scheduler.Schedule(
            self,
            "WriterSchedule",
            schedule_name=schedule_name,
            schedule=scheduler.ScheduleExpression.expression(env_str("CDK_S3_SCHEDULE", "rate(5 minutes)")),
            enabled=env_bool("CDK_S3_SCHEDULE_ENABLED", True),
            description="Run the S3 report writer task",
            target=scheduler_targets.EcsRunFargateTask(
                self.cluster,
                task_definition=self.writer_task,
                capacity_provider_strategies=capacity_provider_strategies(),
                vpc_subnets=task_subnets(config),
                security_groups=[self.task_security_group],
                assign_public_ip=config.tasks_in_public_subnets,
                retry_attempts=2,
            ),
        )

        cdk.CfnOutput(self, "BucketName", value=self.bucket.bucket_name)
        cdk.CfnOutput(self, "WriterTaskDefinitionArn", value=self.writer_task.task_definition_arn)
        cdk.CfnOutput(self, "ReaderTaskDefinitionArn", value=self.reader_task.task_definition_arn)
        cdk.CfnOutput(
            self, "TaskSubnetIds",
            value=cdk.Fn.join(",", self.vpc.select_subnets(subnet_group_name=task_subnets(config).subnet_group_name).subnet_ids),
        )
        cdk.CfnOutput(self, "TaskSecurityGroupId", value=self.task_security_group.security_group_id)
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)

    def _task(self, config, construct_id, purpose, image, command) -> ecs.FargateTaskDefinition:
        family = resource_name(config.product, config.environment, purpose)
        task = ecs.FargateTaskDefinition(
            self, construct_id, family=family, cpu=256, memory_limit_mib=512, runtime_platform=runtime_platform(),
        )
        apply_name_tag(task, family)
        task.add_container(
            "app", container_name="app", image=ecs.ContainerImage.from_registry(image),
            entry_point=["sh", "-c"], command=[command],
            environment={"BUCKET": self.bucket.bucket_name, "AWS_DEFAULT_REGION": self.region},
            logging=log_driver(self, config, purpose, f"{construct_id}LogGroup"),
        )
        return task


STACK_CLASS = S3Stack
