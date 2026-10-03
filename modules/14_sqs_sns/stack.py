"""Module 14 - SQS + SNS: an event-driven pipeline of ECS services (fan-out, DLQs, queue-based scaling).

AWS docs used while writing this module:
- aws_sns / aws_sns_subscriptions (SqsSubscription, filter policies, raw delivery):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_sns_subscriptions/SqsSubscription.html
- aws_sqs Queue (dead-letter queues, visibility timeout, metrics):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_sqs/Queue.html
- aws_ecs README, "Task Auto-Scaling" (auto_scale_task_count, scale_on_metric):
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/README.html
- Amazon SNS message filtering: https://docs.aws.amazon.com/sns/latest/dg/sns-message-filtering.html
- Amazon SQS dead-letter queues: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html
- Scaling based on Amazon SQS: https://docs.aws.amazon.com/autoscaling/application/userguide/application-auto-scaling-step-scaling-policies.html
- Docker Hub - amazon/aws-cli: https://hub.docker.com/r/amazon/aws-cli

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_applicationautoscaling as appscaling
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from shared.config import AppConfig, env_int, env_str
from shared.ecs import build_cluster, fargate_service
from shared.naming import resource_name
from shared.network import build_vpc
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "SqsSnsStack"

AWS_CLI_IMAGE = "amazon/aws-cli:2.37.8"

# Publishes one event every $INTERVAL seconds, alternating "order"/"refund"
# in the `kind` message attribute (the shipping subscription filters on it).
PRODUCER_COMMAND = (
    'i=0; while true; do i=$((i+1)); '
    'if [ $((i % 2)) -eq 0 ]; then kind=refund; else kind=order; fi; '
    'aws sns publish --topic-arn "$TOPIC_ARN" '
    '--message "{\\"id\\":$i,\\"kind\\":\\"$kind\\",\\"from\\":\\"$HOSTNAME\\"}" '
    '--message-attributes "{\\"kind\\":{\\"DataType\\":\\"String\\",\\"StringValue\\":\\"$kind\\"}}" '
    '--query MessageId --output text | sed "s/^/published $kind $i: /"; '
    'sleep "$INTERVAL"; done'
)
# Long-polls its queue, "processes" each message ($WORK_SECONDS), deletes it.
CONSUMER_COMMAND = (
    'while true; do '
    'aws sqs receive-message --queue-url "$QUEUE_URL" --wait-time-seconds 20 --max-number-of-messages 10 '
    '--query "Messages[].[ReceiptHandle,Body]" --output text | '
    'while IFS="$(printf "\\t")" read -r handle body; do '
    '[ -z "$handle" ] || [ "$handle" = "None" ] && continue; '
    'sleep "$WORK_SECONDS"; echo "$CONSUMER processed: $body"; '
    'aws sqs delete-message --queue-url "$QUEUE_URL" --receipt-handle "$handle"; '
    'done; done'
)


class SqsSnsStack(Stack):
    """producer --SNS topic `orders`--> SQS `billing` (all events) and `shipping` (kind=order only).

    - Each queue has a dead-letter queue: a message received
      CDK_SQS_MAX_RECEIVES times without being deleted moves there.
    - Producer and consumers are ECS services running the official AWS CLI
      image; their task roles get exactly the permissions they need
      (sns:Publish; sqs:ReceiveMessage/DeleteMessage...).
    - Each consumer service scales on its queue's backlog
      (ApproximateNumberOfMessagesVisible) with step scaling, between
      CDK_SQS_MIN_CONSUMERS and CDK_SQS_MAX_CONSUMERS tasks.
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

        purpose = "events"
        self.vpc = build_vpc(self, config, purpose)
        self.cluster = build_cluster(self, config, self.vpc, purpose)
        image = env_str("CDK_SQS_IMAGE", AWS_CLI_IMAGE)
        max_receives = env_int("CDK_SQS_MAX_RECEIVES", 3, minimum=1)
        work_seconds = env_int("CDK_SQS_WORK_SECONDS", 2, minimum=0)

        topic_name = resource_name(config.product, config.environment, "sns", "orders")
        self.topic = sns.Topic(self, "OrdersTopic", topic_name=topic_name, display_name="learning-ecs orders")
        apply_name_tag(self.topic, topic_name)

        self.queues: dict[str, sqs.Queue] = {}
        self.consumers: dict[str, ecs.FargateService] = {}
        for name, filter_policy in (
            ("billing", None),
            ("shipping", {"kind": sns.SubscriptionFilter.string_filter(allowlist=["order"])}),
        ):
            queue = self._queue_with_dlq(config, name, max_receives, work_seconds)
            self.topic.add_subscription(
                subscriptions.SqsSubscription(queue, raw_message_delivery=True, filter_policy=filter_policy)
            )
            consumer = fargate_service(
                self, config, self.cluster, construct_id=f"{name.title()}Consumer", purpose=f"{purpose}-{name}",
                image=image, desired=env_int("CDK_SQS_MIN_CONSUMERS", 1, minimum=0),
                entry_point=["sh", "-c"], command=[CONSUMER_COMMAND],
                environment={"QUEUE_URL": queue.queue_url, "CONSUMER": name, "WORK_SECONDS": str(work_seconds),
                             "AWS_DEFAULT_REGION": self.region},
                min_healthy_percent=0,
            )
            queue.grant_consume_messages(consumer.task_definition.task_role)
            self._scale_on_backlog(consumer, queue)
            self.queues[name] = queue
            self.consumers[name] = consumer

        self.producer = fargate_service(
            self, config, self.cluster, construct_id="Producer", purpose=f"{purpose}-producer", image=image,
            desired=env_int("CDK_SQS_PRODUCERS", 1, minimum=0), entry_point=["sh", "-c"], command=[PRODUCER_COMMAND],
            environment={"TOPIC_ARN": self.topic.topic_arn, "AWS_DEFAULT_REGION": self.region,
                         "INTERVAL": str(env_int("CDK_SQS_PUBLISH_INTERVAL_SECONDS", 5, minimum=1))},
            min_healthy_percent=0,
        )
        self.topic.grant_publish(self.producer.task_definition.task_role)

        cdk.CfnOutput(self, "TopicArn", value=self.topic.topic_arn)
        for name, queue in self.queues.items():
            cdk.CfnOutput(self, f"{name.title()}QueueUrl", value=queue.queue_url)
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)

    def _queue_with_dlq(self, config, name, max_receives, work_seconds) -> sqs.Queue:
        dlq_name = resource_name(config.product, config.environment, "sqs", f"{name}-dlq")
        dlq = sqs.Queue(
            self, f"{name.title()}DeadLetterQueue", queue_name=dlq_name, retention_period=Duration.days(14),
            removal_policy=RemovalPolicy.DESTROY,
        )
        apply_name_tag(dlq, dlq_name)
        queue_name = resource_name(config.product, config.environment, "sqs", name)
        queue = sqs.Queue(
            self, f"{name.title()}Queue", queue_name=queue_name,
            # Longer than one message's processing time, so a message being
            # worked on isn't handed to another consumer.
            visibility_timeout=Duration.seconds(max(30, work_seconds * 6)),
            dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=max_receives),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )
        apply_name_tag(queue, queue_name)
        return queue

    def _scale_on_backlog(self, service: ecs.FargateService, queue: sqs.Queue) -> None:
        scaling = service.auto_scale_task_count(
            min_capacity=env_int("CDK_SQS_MIN_CONSUMERS", 1, minimum=0),
            max_capacity=env_int("CDK_SQS_MAX_CONSUMERS", 10, minimum=1),
        )
        scaling.scale_on_metric(
            "Backlog",
            metric=queue.metric_approximate_number_of_messages_visible(period=Duration.minutes(1)),
            # Step scaling: no backlog -> remove a task; growing backlog -> add more.
            scaling_steps=[
                appscaling.ScalingInterval(upper=0, change=-1),
                appscaling.ScalingInterval(lower=10, change=+1),
                appscaling.ScalingInterval(lower=100, change=+3),
                appscaling.ScalingInterval(lower=500, change=+5),
            ],
            adjustment_type=appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            cooldown=Duration.seconds(60),
        )


STACK_CLASS = SqsSnsStack
