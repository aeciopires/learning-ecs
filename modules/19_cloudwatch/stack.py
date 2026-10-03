"""Module 19 - CloudWatch: Container Insights, structured logs, metric filters, alarms, dashboard, Logs Insights.

AWS docs used while writing this module:
- Amazon ECS Container Insights with enhanced observability metrics:
  https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-enhanced-observability-metrics-ECS.html
- Amazon ECS CloudWatch metrics: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/available-metrics.html
- Creating metrics from log events using filters / filter pattern syntax:
  https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/MonitoringLogData.html
  https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/FilterAndPatternSyntax.html
- CloudWatch Logs Insights query syntax:
  https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL_QuerySyntax.html
- Amazon ECS task state change events:
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_task_events.html
- aws_cloudwatch / aws_logs / aws_events_targets READMEs:
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_cloudwatch/README.html
  https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_logs/README.html
- Docker Hub - nginx: https://hub.docker.com/_/nginx ; curlimages/curl: https://hub.docker.com/r/curlimages/curl

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

from typing import cast

import aws_cdk as cdk
from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from shared.apps import LOAD_COMMAND, LOAD_IMAGE, NGINX_CONF, NGINX_IMAGE, NGINX_START
from shared.config import AppConfig, env_int, env_str
from shared.ecs import (
    build_cluster,
    desired_count,
    fargate_service,
    listener_port,
    log_driver,
    retention,
    runtime_platform,
)
from shared.naming import bounded_name, resource_name
from shared.network import PUBLIC, build_vpc, task_subnets
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "CloudWatchStack"

# Saved Logs Insights queries (also used by the dashboard).
QUERIES = {
    "errors-by-path": "fields @timestamp, status, path\n| filter status >= 500\n| stats count(*) as errors by path\n| sort errors desc",
    "slowest-requests": "fields @timestamp, path, status, request_time\n| sort request_time desc\n| limit 20",
    "requests-per-minute": "filter ispresent(status)\n| fields if(status >= 500, 1, 0) as is_error\n"
                           "| stats count(*) as requests, sum(is_error) as errors by bin(1m)",
    "stopped-tasks": "fields @timestamp, detail.group, detail.stopCode, detail.stoppedReason, detail.containers.0.exitCode\n| sort @timestamp desc\n| limit 50",
}


class CloudWatchStack(Stack):
    """A service instrumented end to end with CloudWatch.

    - Container Insights (enhanced by default, CDK_CONTAINER_INSIGHTS) on the cluster.
    - nginx writing structured JSON access logs; metric filters turn them
      into an application 5XX count and a request-time metric.
    - Alarms (application 5XX, unhealthy targets, CPU, running tasks below
      desired) -> one SNS topic (optionally e-mailed).
    - A dashboard with ALB, ECS, Container Insights, log-based metrics, the
      alarms and a Logs Insights widget; saved Logs Insights queries.
    - Every stopped task's event (stop code, reason, exit codes) kept in a
      log group by an EventBridge rule.
    - A load generator task to make all of this move.
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

        purpose = "obs"
        self.vpc = build_vpc(self, config, purpose)
        self.cluster = build_cluster(self, config, self.vpc, purpose)
        count = desired_count("CDK_CLOUDWATCH_DESIRED_COUNT")
        namespace = env_str("CDK_CLOUDWATCH_NAMESPACE", f"{config.product}/{config.environment}")

        # --- the service ------------------------------------------------------------------
        self.service = fargate_service(
            self, config, self.cluster, construct_id="Web", purpose=f"{purpose}-web",
            image=env_str("CDK_CLOUDWATCH_IMAGE", NGINX_IMAGE), container_port=80, desired=count,
            environment={"NGINX_CONF": NGINX_CONF},
            entry_point=["sh", "-c"], command=[NGINX_START],
            # Feeds Container Insights' UnHealthyContainerHealthStatus and RestartCount.
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "wget -q -O /dev/null http://localhost/health || exit 1"],
                interval=Duration.seconds(15), timeout=Duration.seconds(5), retries=3,
                start_period=Duration.seconds(10),
            ),
            container_options={"enable_restart_policy": True, "restart_attempt_period": Duration.seconds(60)},
            health_check_grace_period=Duration.seconds(30),
        )
        self.log_group = cast(logs.LogGroup, self.node.find_child("WebLogGroup"))  # created by fargate_service

        alb_name = bounded_name(config.product, config.environment, "alb", purpose, max_length=32)
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "Alb", load_balancer_name=alb_name, vpc=self.vpc, internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_group_name=PUBLIC),
        )
        apply_name_tag(self.alb, alb_name)
        port = listener_port("CLOUDWATCH")
        listener = self.alb.add_listener("Http", port=port, protocol=elbv2.ApplicationProtocol.HTTP, open=True)
        self.target_group = listener.add_targets(
            "Web",
            target_group_name=bounded_name(config.product, config.environment, "tg", purpose, max_length=32),
            port=80, protocol=elbv2.ApplicationProtocol.HTTP, targets=[self.service],
            health_check=elbv2.HealthCheck(path="/health", healthy_http_codes="200", interval=Duration.seconds(15)),
            deregistration_delay=Duration.seconds(30),
        )

        # --- metrics from logs ---------------------------------------------------------------
        self.errors_metric = logs.MetricFilter(
            self, "Http5xxFilter", log_group=self.log_group, filter_name="http-5xx",
            filter_pattern=logs.FilterPattern.number_value("$.status", ">=", 500),
            metric_namespace=namespace, metric_name="Http5xxCount", metric_value="1", default_value=0,
            unit=cloudwatch.Unit.COUNT,
        ).metric(statistic="Sum", period=Duration.minutes(1))
        self.request_time_metric = logs.MetricFilter(
            self, "RequestTimeFilter", log_group=self.log_group, filter_name="request-time",
            filter_pattern=logs.FilterPattern.exists("$.request_time"),
            metric_namespace=namespace, metric_name="RequestTime", metric_value="$.request_time",
            unit=cloudwatch.Unit.SECONDS,
        ).metric(statistic="p99", period=Duration.minutes(1))

        # --- alarms -> SNS -------------------------------------------------------------------
        topic_name = resource_name(config.product, config.environment, "sns", "alarms")
        self.topic = sns.Topic(self, "AlarmTopic", topic_name=topic_name)
        apply_name_tag(self.topic, topic_name)
        email = env_str("CDK_CLOUDWATCH_ALARM_EMAIL", "")
        if email:
            self.topic.add_subscription(subscriptions.EmailSubscription(email))

        dims = {"ClusterName": self.cluster.cluster_name, "ServiceName": self.service.service_name}
        self.alarms = [
            self._alarm(config, "App5xx", "app-5xx",
                        "5 or more HTTP 5XX in the application logs per minute, 2 minutes in a row",
                        self.errors_metric, threshold=env_int("CDK_CLOUDWATCH_5XX_THRESHOLD", 5, minimum=1),
                        periods=2, missing=cloudwatch.TreatMissingData.NOT_BREACHING),
            self._alarm(config, "UnhealthyTargets", "unhealthy-targets",
                        "at least one target failed its ALB health checks",
                        self.target_group.metrics.unhealthy_host_count(statistic="Minimum", period=Duration.minutes(1)),
                        threshold=1, periods=2, missing=cloudwatch.TreatMissingData.NOT_BREACHING),
            self._alarm(config, "HighCpu", "high-cpu", "service average CPU above 80% for 3 minutes",
                        cloudwatch.Metric(namespace="AWS/ECS", metric_name="CPUUtilization", dimensions_map=dims,
                                          statistic="Average", period=Duration.minutes(1)),
                        threshold=env_int("CDK_CLOUDWATCH_CPU_THRESHOLD", 80, minimum=1), periods=3,
                        missing=cloudwatch.TreatMissingData.MISSING),
            self._alarm(config, "TasksBelowDesired", "tasks-below-desired",
                        f"fewer than {count} running tasks for 3 minutes (Container Insights)",
                        cloudwatch.Metric(namespace="ECS/ContainerInsights", metric_name="RunningTaskCount",
                                          dimensions_map=dims, statistic="Minimum", period=Duration.minutes(1)),
                        threshold=count, periods=3, missing=cloudwatch.TreatMissingData.BREACHING,
                        operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD),
        ]

        # --- stopped tasks -> a log group, for post-mortems ---------------------------------------
        events_log_name = f"/aws/events/{config.product}/{config.environment}/{purpose}-stopped-tasks"
        self.events_log_group = logs.LogGroup(
            self, "StoppedTasksLogGroup", log_group_name=events_log_name, retention=retention(config),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        apply_name_tag(self.events_log_group, events_log_name)
        rule_name = resource_name(config.product, config.environment, "rule", purpose, "stopped-tasks")
        self.stopped_tasks_rule = events.Rule(
            self, "StoppedTasksRule", rule_name=rule_name,
            description="Every task of the cluster that stops, with its stop code and reason",
            event_pattern=events.EventPattern(
                source=["aws.ecs"], detail_type=["ECS Task State Change"],
                detail={"clusterArn": [self.cluster.cluster_arn], "lastStatus": ["STOPPED"]},
            ),
            targets=[targets.CloudWatchLogGroup(self.events_log_group)],
        )

        # --- saved queries and the dashboard ------------------------------------------------------
        for name, query in QUERIES.items():
            group = self.events_log_group if name == "stopped-tasks" else self.log_group
            logs.CfnQueryDefinition(
                self, f"Query{name.title().replace('-', '')}",
                name=f"{config.product}/{config.environment}/{name}", query_string=query,
                log_group_names=[group.log_group_name],
            )
        self.dashboard = self._dashboard(config, dims)

        # --- load generator ------------------------------------------------------------------------
        family = resource_name(config.product, config.environment, f"{purpose}-load")
        self.load_task = ecs.FargateTaskDefinition(
            self, "LoadTaskDefinition", family=family, cpu=256, memory_limit_mib=512, runtime_platform=runtime_platform(),
        )
        apply_name_tag(self.load_task, family)
        self.load_task.add_container(
            "app", container_name="app", image=ecs.ContainerImage.from_registry(LOAD_IMAGE),
            entry_point=["sh", "-c"], command=[LOAD_COMMAND],
            environment={
                "URL": f"http://{self.alb.load_balancer_dns_name}:{port}/",
                "WORKERS": str(env_int("CDK_CLOUDWATCH_LOAD_WORKERS", 5, minimum=1)),
                "DURATION": str(env_int("CDK_CLOUDWATCH_LOAD_SECONDS", 300, minimum=1)),
                "ERROR_EVERY": str(env_int("CDK_CLOUDWATCH_LOAD_ERROR_EVERY", 20, minimum=1)),
            },
            logging=log_driver(self, config, f"{purpose}-load", "LoadLogGroup"),
        )
        self.load_security_group = ec2.SecurityGroup(
            self, "LoadSecurityGroup", vpc=self.vpc, description="load generator (egress only)"
        )

        cdk.CfnOutput(self, "Url", value=f"http://{self.alb.load_balancer_dns_name}:{port}/")
        cdk.CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        cdk.CfnOutput(self, "ServiceName", value=self.service.service_name)
        cdk.CfnOutput(self, "LogGroupName", value=self.log_group.log_group_name)
        cdk.CfnOutput(self, "StoppedTasksLogGroupName", value=events_log_name)
        cdk.CfnOutput(self, "AlarmTopicArn", value=self.topic.topic_arn)
        cdk.CfnOutput(self, "DashboardName", value=self.dashboard.dashboard_name)
        cdk.CfnOutput(self, "LoadTaskDefinitionArn", value=self.load_task.task_definition_arn)
        cdk.CfnOutput(
            self, "TaskSubnetIds",
            value=cdk.Fn.join(",", self.vpc.select_subnets(subnet_group_name=task_subnets(config).subnet_group_name).subnet_ids),
        )
        cdk.CfnOutput(self, "LoadSecurityGroupId", value=self.load_security_group.security_group_id)

    def _alarm(self, config, construct_id, name, description, metric, *, threshold, periods, missing,
               operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD) -> cloudwatch.Alarm:
        alarm = cloudwatch.Alarm(
            self, f"{construct_id}Alarm",
            alarm_name=resource_name(config.product, config.environment, "obs", name),
            alarm_description=description, metric=metric, threshold=threshold,
            evaluation_periods=periods, comparison_operator=operator, treat_missing_data=missing,
        )
        action = cw_actions.SnsAction(self.topic)
        alarm.add_alarm_action(action)
        alarm.add_ok_action(action)
        return alarm

    def _dashboard(self, config, dims) -> cloudwatch.Dashboard:
        name = resource_name(config.product, config.environment, "obs")
        tg = self.target_group.metrics
        insights = {
            metric: cloudwatch.Metric(namespace="ECS/ContainerInsights", metric_name=metric, dimensions_map=dims,
                                      statistic="Average", period=Duration.minutes(1))
            for metric in ("RunningTaskCount", "DesiredTaskCount", "PendingTaskCount",
                           "TaskCpuUtilization", "TaskMemoryUtilization")
        }
        ecs_metric = {
            metric: cloudwatch.Metric(namespace="AWS/ECS", metric_name=metric, dimensions_map=dims,
                                      statistic="Average", period=Duration.minutes(1))
            for metric in ("CPUUtilization", "MemoryUtilization")
        }
        dashboard = cloudwatch.Dashboard(self, "Dashboard", dashboard_name=name, default_interval=Duration.hours(3))
        dashboard.add_widgets(
            cloudwatch.AlarmStatusWidget(title="Alarms", alarms=self.alarms, width=24, height=3),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="Requests and errors (ALB)", width=8, left=[tg.request_count()],
                                   right=[tg.http_code_target(elbv2.HttpCodeTarget.TARGET_5XX_COUNT),
                                          self.alb.metrics.http_code_elb(elbv2.HttpCodeElb.ELB_5XX_COUNT)]),
            cloudwatch.GraphWidget(title="Response time (ALB, seconds)", width=8,
                                   left=[tg.target_response_time(statistic="p50"),
                                         tg.target_response_time(statistic="p99")]),
            cloudwatch.GraphWidget(title="Application 5XX and p99 request time (from logs)", width=8,
                                   left=[self.errors_metric], right=[self.request_time_metric]),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="Tasks (Container Insights)", width=8,
                                   left=[insights["RunningTaskCount"], insights["DesiredTaskCount"],
                                         insights["PendingTaskCount"]]),
            cloudwatch.GraphWidget(title="Service CPU / memory % (AWS/ECS)", width=8,
                                   left=[ecs_metric["CPUUtilization"], ecs_metric["MemoryUtilization"]]),
            cloudwatch.GraphWidget(title="Task CPU / memory % (Container Insights)", width=8,
                                   left=[insights["TaskCpuUtilization"], insights["TaskMemoryUtilization"]]),
        )
        dashboard.add_widgets(
            cloudwatch.LogQueryWidget(title="Errors by path (Logs Insights)", width=12,
                                      log_group_names=[self.log_group.log_group_name],
                                      query_string=QUERIES["errors-by-path"]),
            cloudwatch.LogQueryWidget(title="Stopped tasks", width=12,
                                      log_group_names=[self.events_log_group.log_group_name],
                                      query_string=QUERIES["stopped-tasks"]),
        )
        return dashboard


STACK_CLASS = CloudWatchStack
