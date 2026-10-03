<!-- TOC -->

- [Metrics](#metrics)
  - [Where ECS metrics come from](#where-ecs-metrics-come-from)
  - [The four golden signals for an ECS service](#the-four-golden-signals-for-an-ecs-service)
  - [AWS/ECS](#awsecs)
  - [ECS/ContainerInsights](#ecscontainerinsights)
  - [Load balancers](#load-balancers)
  - [API Gateway, CloudFront, Service Connect](#api-gateway-cloudfront-service-connect)
  - [Data services](#data-services)
  - [Messaging, storage and schedules](#messaging-storage-and-schedules)
  - [Scaling and deployments](#scaling-and-deployments)
  - [Prometheus (module 20)](#prometheus-module-20)
  - [Datadog (module 21)](#datadog-module-21)
  - [The same signal in CloudWatch, Prometheus and Datadog](#the-same-signal-in-cloudwatch-prometheus-and-datadog)
  - [Querying metrics from the command line](#querying-metrics-from-the-command-line)
  - [References](#references)

<!-- TOC -->

# Metrics

A reference for every metric this learning path uses: what each namespace
or tool offers for ECS, the dimensions you need to query it, and how the
same signal looks in CloudWatch, Prometheus and Datadog. Each module's
"Metrics to watch" section says which ones matter for that module.

On floci (2.1.0) no AWS service publishes metrics - the CloudWatch API
works, but the data is what you push yourself (module 16). Everything
below is real AWS.

## Where ECS metrics come from

| Source | Namespace / tool | Cost | Granularity | Turned on by | Module |
|---|---|---|---|---|---|
| ECS service metrics | `AWS/ECS` | included | cluster, service; 1 minute; only while tasks run | always on | 02 |
| Container Insights (standard) | `ECS/ContainerInsights` | billed (custom metrics + performance logs) | cluster, service, task family | cluster setting `containerInsights=enabled` | 19 |
| Container Insights (enhanced) | `ECS/ContainerInsights` | billed | + task and container | `containerInsights=enhanced` (`CDK_CONTAINER_INSIGHTS`) | 19 |
| Load balancers, data services, queues... | `AWS/ApplicationELB`, `AWS/RDS`, `AWS/SQS`, ... | included | per resource | always on | 04-15 |
| Your logs | custom namespace (metric filters) | billed as custom metrics | what you extract | `put-metric-filter` | 19 |
| Your application, Prometheus format | Prometheus / Amazon Managed Service for Prometheus | your Prometheus, or AMP pricing | whatever the app exposes | ADOT collector sidecar | 20 |
| Datadog Agent | `ecs.fargate.*`, integrations | Datadog pricing | container | Agent sidecar | 21 |

## The four golden signals for an ECS service

| Signal | CloudWatch | Watch for |
|---|---|---|
| Traffic | `AWS/ApplicationELB` `RequestCount` | sudden drops (clients can't reach you) as much as spikes |
| Errors | `HTTPCode_Target_5XX_Count` (the app), `HTTPCode_ELB_5XX_Count` (the load balancer: no healthy targets, timeouts) | any sustained rate |
| Latency | `TargetResponseTime` p50/p99 | p99 more than p50 |
| Saturation | `AWS/ECS` `CPUUtilization`/`MemoryUtilization`; `ECS/ContainerInsights` `RunningTaskCount` vs `DesiredTaskCount` | sustained high usage; running below desired |

## AWS/ECS

[Amazon ECS CloudWatch metrics](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/available-metrics.html)
- 1-minute data, only for resources with running tasks.

| Metric | Dimensions | Unit | Notes |
|---|---|---|---|
| `CPUUtilization`, `MemoryUtilization` | `ClusterName`, `ServiceName` (Fargate and EC2); `ClusterName` (EC2) | Percent | service average - the input of target tracking (module 16) |
| `CPUReservation`, `MemoryReservation`, `GPUReservation` | `ClusterName` | Percent | EC2 capacity only (module 03) |
| `EBSFilesystemUtilization` | `ClusterName`, `ServiceName` | Percent | tasks with EBS volumes |
| Service Connect metrics | see [below](#api-gateway-cloudfront-service-connect) | | module 08 |

`CapacityProviderReservation` (namespace `AWS/ECS/ManagedScaling`) drives
EC2 managed scaling (module 03): instances needed / instances running x 100.

## ECS/ContainerInsights

[Container Insights metrics for Amazon ECS](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-metrics-ECS.html)
and [with enhanced observability](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-enhanced-observability-metrics-ECS.html):

| Metric | Dimensions (main sets) | Notes |
|---|---|---|
| `RunningTaskCount`, `PendingTaskCount`, `DesiredTaskCount`, `DeploymentCount`, `TaskSetCount` | `ServiceName`, `ClusterName` | running below desired = tasks failing to start; `DeploymentCount` > 1 = a deployment in progress |
| `TaskCount`, `ServiceCount`, `ContainerInstanceCount` | `ClusterName` | cluster inventory |
| `CpuUtilized`/`CpuReserved` (CPU units), `MemoryUtilized`/`MemoryReserved` (MB) | `TaskDefinitionFamily`+`ClusterName`; `ServiceName`+`ClusterName`; `ClusterName`; enhanced: + `TaskId` | utilization vs reservation = right-sizing |
| `NetworkRxBytes`, `NetworkTxBytes` (Bytes/s), `StorageReadBytes`, `StorageWriteBytes` | as above | `awsvpc` or `bridge` network modes |
| `EphemeralStorageReserved`, `EphemeralStorageUtilized` (GB) | as above | Fargate platform 1.4.0+ |
| enhanced: `TaskCpuUtilization`, `TaskMemoryUtilization`, `TaskEphemeralStorageUtilization` (Percent) | + `TaskId` | per task |
| enhanced: `ContainerCpuUtilization`, `ContainerMemoryUtilization` (Percent), `ContainerCpuUtilized`, `ContainerMemoryUtilized`, `ContainerNetworkRxBytes`, ... | `ContainerName` with `ServiceName`/`TaskDefinitionFamily`/`TaskId` + `ClusterName` | per container - sidecars included |
| `RestartCount` | many sets, down to the container | only containers with a **restart policy** |
| enhanced: `UnHealthyContainerHealthStatus` | down to the container | only containers with a **health check**; 1 = unhealthy |

Performance log events behind these metrics are in
`/aws/ecs/containerinsights/<cluster>/performance`, queryable with Logs
Insights.

## Load balancers

[ALB metrics](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-cloudwatch-metrics.html)
(`AWS/ApplicationELB`) - reported only while requests flow; health checks
excluded. Dimension values are the end of the ARN: `LoadBalancer=app/<name>/<id>`,
`TargetGroup=targetgroup/<name>/<id>`.

| Metric | Dimensions | Notes |
|---|---|---|
| `RequestCount` (Sum) | `LoadBalancer`; `LoadBalancer`,`TargetGroup`; `LoadBalancer`,`AvailabilityZone` | traffic |
| `RequestCountPerTarget` (Sum) | `TargetGroup` (required) | the input of `ALBRequestCountPerTarget` scaling |
| `TargetResponseTime` (seconds; Average, pNN) | `LoadBalancer`; `TargetGroup`,`LoadBalancer` | latency from the load balancer to the task |
| `HTTPCode_Target_2XX/3XX/4XX/5XX_Count` | as `RequestCount` | answers from the tasks |
| `HTTPCode_ELB_4XX/5XX_Count`, `HTTPCode_ELB_500/502/503/504_Count` | `LoadBalancer` | generated by the ALB (502: bad response from a target; 503: no registered/healthy targets; 504: target timeout) |
| `HealthyHostCount`, `UnHealthyHostCount` | `TargetGroup`,`LoadBalancer` | alarm on `UnHealthyHostCount` Minimum > 0 |
| `TargetConnectionErrorCount`, `RejectedConnectionCount`, `ActiveConnectionCount`, `NewConnectionCount`, `ConsumedLCUs` | `LoadBalancer` | connection problems and cost |

[NLB metrics](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-cloudwatch-metrics.html)
(`AWS/NetworkELB`, `LoadBalancer=net/<name>/<id>`): `ActiveFlowCount`,
`NewFlowCount`, `ProcessedBytes`, `TCP_Client_Reset_Count`,
`TCP_ELB_Reset_Count`, `TCP_Target_Reset_Count`, `HealthyHostCount`/`UnHealthyHostCount`
(`LoadBalancer`,`TargetGroup`), `PortAllocationErrorCount` (client IP
preservation off), `RejectedFlowCount`, `SecurityGroupBlockedFlowCount_Inbound_TCP`.

## API Gateway, CloudFront, Service Connect

| Service | Namespace | Metrics | Dimensions |
|---|---|---|---|
| REST API (module 06) | `AWS/ApiGateway` | `Count`, `4XXError`, `5XXError` (Average = rate), `Latency`, `IntegrationLatency` (ms) | `ApiName`; `ApiName`,`Stage`; per method with detailed metrics |
| HTTP API (module 06) | `AWS/ApiGateway` | `Count`, `4xx`, `5xx`, `Latency`, `IntegrationLatency`, `DataProcessed` | `ApiId`; `ApiId`,`Stage` |
| CloudFront (modules 07, 18) | `AWS/CloudFront`, read in **us-east-1** | `Requests`, `BytesDownloaded`, `4xxErrorRate`, `5xxErrorRate`, `TotalErrorRate`; additional (paid): `OriginLatency`, `CacheHitRate`, per-code error rates | `DistributionId`, `Region=Global` |
| Service Connect (module 08) | `AWS/ECS` | `RequestCount` (needs `appProtocol`), `HTTPCode_Target_2XX..5XX_Count`, `TargetResponseTime` (ms), `ActiveConnectionCount`, `NewConnectionCount`, `ProcessedBytes` | `DiscoveryName` / `TargetDiscoveryName`, with `ServiceName`,`ClusterName` |

## Data services

| Service | Namespace | Key metrics | Dimensions |
|---|---|---|---|
| RDS (modules 09, 10) | `AWS/RDS` | `CPUUtilization`, `DatabaseConnections`, `FreeStorageSpace`, `FreeableMemory`, `SwapUsage`, `ReadLatency`/`WriteLatency`, `ReadIOPS`/`WriteIOPS`, `DiskQueueDepth`, `CPUCreditBalance` (t classes), `ReplicaLag` | `DBInstanceIdentifier` |
| Aurora (module 11) | `AWS/RDS` | `AuroraReplicaLag`, `AuroraReplicaLagMaximum`, `ServerlessDatabaseCapacity`, `ACUUtilization`, `CommitLatency`, `Deadlocks`, `BufferCacheHitRatio`, `VolumeBytesUsed` | `DBClusterIdentifier` (+ `Role`), `DBInstanceIdentifier` |
| ElastiCache Valkey/Redis OSS (module 12) | `AWS/ElastiCache` | `EngineCPUUtilization`, `CPUUtilization`, `DatabaseMemoryUsagePercentage`, `Evictions`, `CurrConnections`, `ReplicationLag`, `CacheHits`/`CacheMisses`, `TrafficManagementActive` | `CacheClusterId` (+ `CacheNodeId`), `ReplicationGroupId` |
| DocumentDB (module 13) | `AWS/DocDB` | `CPUUtilization`, `DatabaseConnections`, `DBInstanceReplicaLag`, `BufferCacheHitRatio`, `OpcountersInsert`/`Query`, `VolumeBytesUsed`, `LowMemNumOperationsThrottled` | `DBClusterIdentifier`, `DBInstanceIdentifier` |

Connections deserve a special look on ECS: every task opens its own pool,
so scaling a service out multiplies `DatabaseConnections` /
`CurrConnections`.

## Messaging, storage and schedules

| Service | Namespace | Key metrics | Dimensions |
|---|---|---|---|
| SQS (module 14) | `AWS/SQS` | `ApproximateNumberOfMessagesVisible` (backlog - the scaling input), `ApproximateAgeOfOldestMessage`, `NumberOfMessagesSent`/`Received`/`Deleted`, `NumberOfEmptyReceives`; the DLQ's `ApproximateNumberOfMessagesVisible` > 0 | `QueueName` |
| SNS (module 14) | `AWS/SNS` | `NumberOfMessagesPublished`, `NumberOfNotificationsDelivered`, `NumberOfNotificationsFailed`, `NumberOfNotificationsFilteredOut` | `TopicName` |
| S3 (module 15) | `AWS/S3` | daily: `BucketSizeBytes` (`StorageType=StandardStorage`), `NumberOfObjects` (`AllStorageTypes`); with a request metrics configuration: `AllRequests`, `GetRequests`, `PutRequests`, `4xxErrors`, `5xxErrors`, `FirstByteLatency`, `TotalRequestLatency` | `BucketName`, `StorageType` / `FilterId` |
| EventBridge Scheduler (module 15) | `AWS/Scheduler` | `InvocationAttemptCount`, `TargetErrorCount`, `TargetErrorThrottledCount`, `InvocationThrottleCount`, `InvocationDroppedCount` | `ScheduleGroup` |

## Scaling and deployments

- **Target tracking** (module 16) creates two alarms per policy,
  `TargetTracking-<resource>-AlarmHigh-...` / `-AlarmLow-...`, on the
  metric behind the predefined type: `ECSServiceAverageCPUUtilization` ->
  `AWS/ECS CPUUtilization`, `ECSServiceAverageMemoryUtilization` ->
  `MemoryUtilization`, `ALBRequestCountPerTarget` ->
  `AWS/ApplicationELB RequestCountPerTarget`. Scaling activities:
  `aws application-autoscaling describe-scaling-activities`.
- **Deployments** (module 17) aren't metrics but events: EventBridge
  `ECS Deployment State Change` (`SERVICE_DEPLOYMENT_IN_PROGRESS`,
  `_COMPLETED`, `_FAILED`) and `ECS Task State Change` (module 19 keeps
  the stopped ones). `DeploymentCount` in Container Insights shows them as
  a metric.

## Prometheus (module 20)

| Metric | Source | Labels |
|---|---|---|
| `caddy_http_request_duration_seconds_bucket`/`_count`/`_sum` | the app (Caddy), scraped by the ADOT sidecar | `code`, `method`, `handler`, `server`, plus `job`, `host_name` and the ECS resource attributes added by the collector |
| `caddy_http_requests_total`, `caddy_http_requests_in_flight` | the app | `handler`, `server` |
| `up{job="caddy"}` | written by the collector's Prometheus receiver | 0 = scrape failed |
| `ecs.task.cpu.utilized`, `ecs.task.memory.utilized`, `ecs.task.network.rate.rx/tx`, ... | ADOT `awsecscontainermetrics` receiver (task metadata endpoint) | converted to Prometheus-style names on remote write; list them with `/api/v1/label/__name__/values` |
| `prometheus_tsdb_head_series`, `prometheus_rule_evaluation_failures_total` | the Prometheus server itself | cardinality, broken rules |

PromQL for these: [module 20, "PromQL for an ECS service"](../modules/20_prometheus_grafana/README.md#promql-for-an-ecs-service).

## Datadog (module 21)

| Metric | Source | Notes |
|---|---|---|
| `ecs.fargate.cpu.percent`, `ecs.fargate.cpu.usage`, `ecs.fargate.cpu.limit` | Agent, `ECS_FARGATE=true` | per container (`container_name`, `task_arn` tags) |
| `ecs.fargate.mem.usage`, `ecs.fargate.mem.limit`, `ecs.fargate.mem.max_usage` | Agent | bytes |
| `ecs.fargate.io.bytes.read`/`write`, `ecs.fargate.io.ops.read`/`write`, `ecs.fargate.net.bytes_rcvd`/`sent` | Agent | disk and network (network: Fargate 1.4.0+) |
| `fargate_check` (service check) | Agent | `CRITICAL` when the Agent can't reach the Fargate metadata |
| `nginx.net.request_per_s`, `nginx.connections.active`, `nginx.requests.total` | NGINX check via Autodiscovery | |
| Tags `env`, `service`, `version` | unified service tagging | filter every metric, log and trace by them |

## The same signal in CloudWatch, Prometheus and Datadog

| Signal | CloudWatch | Prometheus (module 20) | Datadog (module 21) |
|---|---|---|---|
| Service CPU | `AWS/ECS CPUUtilization` (`ClusterName`,`ServiceName`) | ADOT `ecs.task.cpu.utilized` (per task) | `ecs.fargate.cpu.percent` |
| Memory | `AWS/ECS MemoryUtilization`; `ECS/ContainerInsights TaskMemoryUtilization` | ADOT `ecs.task.memory.utilized` | `ecs.fargate.mem.usage` / `mem.limit` |
| Requests per second | `AWS/ApplicationELB RequestCount` | `sum(rate(caddy_http_request_duration_seconds_count[1m]))` | `nginx.net.request_per_s` |
| Error rate | `HTTPCode_Target_5XX_Count` / `RequestCount` | `sum(rate(..._count{code=~"5.."}[5m])) / sum(rate(..._count[5m]))` | logs `status:>=500` for the service |
| Latency p99 | `TargetResponseTime` p99 | `histogram_quantile(0.99, sum by (le) (rate(..._bucket[5m])))` | APM (instrumented apps) |
| Tasks running | `ECS/ContainerInsights RunningTaskCount` | `sum(up{job="caddy"})` | number of reporting tasks for the service |
| Logs | CloudWatch Logs + Logs Insights | (not Prometheus - use CloudWatch Logs or Loki) | FireLens -> Datadog logs |

## Querying metrics from the command line

```bash
# what exists (only metrics with data in the last 2 weeks are listed)
aws cloudwatch list-metrics --namespace AWS/ECS --dimensions Name=ClusterName,Value=learning-ecs-dev-ecs-alb

# one metric, several statistics
aws cloudwatch get-metric-statistics --namespace AWS/ECS --metric-name CPUUtilization \
  --dimensions Name=ClusterName,Value=learning-ecs-dev-ecs-alb Name=ServiceName,Value=learning-ecs-dev-alb-web \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Average Maximum --output table

# percentiles
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name TargetResponseTime \
  --dimensions Name=LoadBalancer,Value=app/<name>/<id> \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --extended-statistics p50 p99

# metric math: 5XX error rate in percent
aws cloudwatch get-metric-data \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --metric-data-queries '[
    {"Id":"errors","ReturnData":false,"MetricStat":{"Metric":{"Namespace":"AWS/ApplicationELB","MetricName":"HTTPCode_Target_5XX_Count","Dimensions":[{"Name":"LoadBalancer","Value":"app/<name>/<id>"}]},"Period":60,"Stat":"Sum"}},
    {"Id":"requests","ReturnData":false,"MetricStat":{"Metric":{"Namespace":"AWS/ApplicationELB","MetricName":"RequestCount","Dimensions":[{"Name":"LoadBalancer","Value":"app/<name>/<id>"}]},"Period":60,"Stat":"Sum"}},
    {"Id":"error_rate","Expression":"100 * FILL(errors, 0) / requests","Label":"5XX %"}
  ]'

# Prometheus HTTP API (module 20)
curl -s -G http://localhost:8096/api/v1/query --data-urlencode 'query=sum(up{job="caddy"})'
```

The `LoadBalancer`/`TargetGroup` dimension values are the end of their
ARNs: `${ARN#*:loadbalancer/}` and `${ARN##*:}` in bash.

## References

- [Amazon ECS CloudWatch metrics](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/available-metrics.html)
- [Container Insights for Amazon ECS](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-metrics-ECS.html) · [enhanced observability](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-enhanced-observability-metrics-ECS.html)
- [ALB metrics](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-cloudwatch-metrics.html) · [NLB metrics](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-cloudwatch-metrics.html)
- [Using metric math](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/using-metric-math.html)
- Each module's "Metrics to watch" section links the service-specific metric pages.
- [Datadog - Amazon ECS on AWS Fargate](https://docs.datadoghq.com/integrations/ecs_fargate/) · [Caddy metrics](https://caddyserver.com/docs/metrics)
