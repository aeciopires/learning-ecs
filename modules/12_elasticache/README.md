<!-- TOC -->

- [Module 12 - ElastiCache (Valkey) read and written by ECS tasks](#module-12---elasticache-valkey-read-and-written-by-ecs-tasks)
  - [Overview](#overview)
  - [What you will learn](#what-you-will-learn)
  - [Architecture](#architecture)
  - [AWS services and CDK constructs used](#aws-services-and-cdk-constructs-used)
  - [Configuration](#configuration)
  - [Prerequisites](#prerequisites)
  - [Tests](#tests)
  - [Deploy with floci (local, free)](#deploy-with-floci-local-free)
  - [Deploy to real AWS (optional)](#deploy-to-real-aws-optional)
  - [Verify](#verify)
    - [List every resource with the AWS CLI](#list-every-resource-with-the-aws-cli)
  - [Manage it with the AWS CLI](#manage-it-with-the-aws-cli)
  - [Metrics to watch](#metrics-to-watch)
  - [Troubleshooting](#troubleshooting)
  - [floci vs real AWS](#floci-vs-real-aws)
  - [Clean up](#clean-up)
  - [Notes and cautions](#notes-and-cautions)
  - [References](#references)

<!-- TOC -->

# Module 12 - ElastiCache (Valkey) read and written by ECS tasks

## Overview

Tasks are disposable; anything they share - sessions, rate-limit
counters, cached query results, locks - has to live outside them. This
module puts **Amazon ElastiCache for Valkey** (the open-source,
Redis-compatible engine; Redis OSS is a switch away) next to an ECS
cluster and shows two consumers using it:

- **`counter`**, a service with two tasks that each run a loop with the
  official `valkey-cli` from the [`valkey/valkey`](https://hub.docker.com/r/valkey/valkey)
  image: `INCR visits` on the **primary endpoint**, then `GET visits` from
  the **reader endpoint** (the replicas) - two tasks, one shared counter;
- **`*-elasticache-client`**, a one-off task definition that runs any
  command you pass.

The cache is a **replication group** with a primary and one replica in
another AZ, **Multi-AZ with automatic failover**, encrypted in transit
(TLS) and at rest, reachable only from the two task security groups.

## What you will learn

- ElastiCache with L1 constructs (`CfnReplicationGroup`, `CfnSubnetGroup`)
  - there is no stable L2 - and the CloudFormation attributes for its
  endpoints.
- Primary vs reader endpoint; Multi-AZ and automatic failover (both need
  at least one replica).
- TLS from a container: `valkey-cli --tls --cacert <bundle>`.
- A "bring your own cache" switch (`CDK_CACHE_ENDPOINT`), which is also
  how the module runs on floci.

## Architecture

```
 counter tasks (x2, no load balancer)          one-off client task (aws ecs run-task)
   INCR visits  -> primary endpoint  --+         valkey-cli $CMD  -> primary endpoint
   GET  visits  <- reader endpoint   --+--------------------------+
                                       |  6379, TLS, only from the two task SGs
                                       v
   ElastiCache replication group learning-ecs-dev-valkey   (isolated subnets)
     primary (AZ a)  ---async replication--->  replica (AZ b)   [Multi-AZ, automatic failover]
```

## AWS services and CDK constructs used

| AWS service | CDK construct (Python) | Level |
|---|---|---|
| Amazon ElastiCache | `aws_cdk.aws_elasticache.CfnReplicationGroup` | **L1** - no stable L2 exists ([API reference](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_elasticache/CfnReplicationGroup.html)) |
| Amazon ElastiCache | `aws_cdk.aws_elasticache.CfnSubnetGroup` | **L1** ([API reference](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_elasticache/CfnSubnetGroup.html)) |
| Amazon ECS | `FargateService` (worker, no load balancer), `FargateTaskDefinition` (client) | L2 |
| Amazon VPC | `SecurityGroup` (the cache's) | L2 |

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CDK_CACHE_ENGINE` | `valkey` | `valkey` or `redis` |
| `CDK_CACHE_ENGINE_VERSION` | `8.1` (valkey) / `7.1` (redis) | engine version |
| `CDK_CACHE_NODE_TYPE` | `cache.t4g.micro` | node type |
| `CDK_CACHE_REPLICAS` | `1` | replicas (`0` = a single node, no failover) |
| `CDK_CACHE_TLS` | `true` | encryption in transit (`.env.example`: `false`, for floci) |
| `CDK_CACHE_PORT` | `6379` | cache port |
| `CDK_CACHE_SNAPSHOT_DAYS` | `1` | automatic backup retention (`0` = none) |
| `CDK_CACHE_ENDPOINT` | unset (`.env.example`: `floci`) | use an existing cache at this host instead of creating one |
| `CDK_CACHE_INTERVAL_SECONDS` | `10` | how often each counter task writes/reads |
| `CDK_CACHE_DESIRED_COUNT` | `CDK_DESIRED_COUNT` (`2`) | counter tasks |
| `CDK_CACHE_CLIENT_IMAGE` | `valkey/valkey:8.1-alpine` | image of both consumers |

## Prerequisites

[Module 02](../02_fargate_service/README.md). floci running and `.env`
loaded (with the two floci lines for this module - see below).

## Tests

[`../../tests/unit/test_12_elasticache.py`](../../tests/unit/test_12_elasticache.py)
checks the Multi-AZ Valkey replication group (failover, encryption, node
type), that zero replicas disables failover, that tasks write to the
primary and read from the reader endpoint over TLS, that only the two task
security groups reach the cache, the bring-your-own-endpoint mode, the
Redis OSS engine, invalid engines, and the mandatory tags:

```bash
uv run pytest tests/unit/test_12_elasticache.py -v
```

## Deploy with floci (local, free)

floci's CloudFormation doesn't create ElastiCache resources (they are
stubbed - [`../../REQUIREMENTS.md`, section 10](../../REQUIREMENTS.md#10-floci-vs-real-aws)),
but its ElastiCache API does - as a real Valkey container, reachable from
ECS tasks at `floci:6379`, without TLS. So on floci you create the cache
with the AWS CLI and point the stack at it; `.env.example` already sets
`CDK_CACHE_ENDPOINT=floci` and `CDK_CACHE_TLS=false`:

```bash
aws elasticache create-replication-group --replication-group-id learning-ecs-dev-valkey \
  --replication-group-description "learning-ecs dev valkey" --engine valkey \
  --cache-node-type cache.t4g.micro --num-cache-clusters 2
aws elasticache wait replication-group-available --replication-group-id learning-ecs-dev-valkey

uv run cdk bootstrap
uv run cdk synth ElastiCacheStack
uv run cdk diff ElastiCacheStack
uv run cdk deploy ElastiCacheStack --require-approval never --method=direct
```

## Deploy to real AWS (optional)

**Each cache node bills per hour** (two with the default replica) - see
[Amazon ElastiCache pricing](https://aws.amazon.com/elasticache/pricing/).
Creating the replication group takes several minutes.

```bash
unset AWS_ENDPOINT_URL CDK_CACHE_ENDPOINT CDK_CACHE_TLS   # REQUIREMENTS.md section 9.2
uv run cdk bootstrap --profile <your-aws-cli-profile>
uv run cdk diff ElastiCacheStack --profile <your-aws-cli-profile>
uv run cdk deploy ElastiCacheStack --profile <your-aws-cli-profile>
```

## Verify

The counter tasks print what they wrote and read every 10 seconds:

```bash
aws logs tail /ecs/learning-ecs/dev/elasticache-counter --follow                                    # real AWS
docker logs learning-ecs-floci 2>&1 | grep 'ecs:learning-ecs-dev-elasticache-counter' | tail -4    # floci
# task=db31b20abf57 wrote visits=1 read-from-replica visits=1
# task=c8708e15f0eb wrote visits=2 read-from-replica visits=2   <- the other task, same counter
```

Any command from the one-off client task (`CMD`; the default is
`INFO replication`, which shows the primary and its replicas):

```bash
out() { aws cloudformation describe-stacks --stack-name ElastiCacheStack \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
CLUSTER=$(out ClusterName); SG=$(out ClientSecurityGroupId); TD=$(out ClientTaskDefinitionArn)
SUBNETS=$(aws ecs describe-services --cluster "$CLUSTER" --services learning-ecs-dev-elasticache-counter \
  --query 'services[0].networkConfiguration.awsvpcConfiguration.subnets' --output text | tr '\t' ',')
TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$TD" \
  --capacity-provider-strategy capacityProvider=FARGATE,weight=1 \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"client","environment":[{"name":"CMD","value":"GET visits"}]}]}' \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
aws logs tail /ecs/learning-ecs/dev/elasticache-client --since 5m                                  # real AWS
docker logs learning-ecs-floci 2>&1 | grep 'ecs:learning-ecs-dev-elasticache-client' | tail -2    # floci
```

The cache itself:

```bash
aws elasticache describe-replication-groups --replication-group-id learning-ecs-dev-valkey \
  --query 'ReplicationGroups[0].{status:Status,primary:NodeGroups[0].PrimaryEndpoint,reader:NodeGroups[0].ReaderEndpoint,members:MemberClusters,failover:AutomaticFailover,multiAZ:MultiAZ,tls:TransitEncryptionEnabled}'
```

<!-- BEGIN resource-commands (generated by scripts/resource_commands.py) -->
### List every resource with the AWS CLI

Every resource this stack creates, one `aws` command each, parametrized
by environment (`ENV`), region (`REGION`) and - where a command builds an
ARN - account (`ACCOUNT`): set them to match your deployment, with your
`.env` loaded (floci) or your AWS profile active (real AWS). A resource
without a name of its own is looked up through the stack by its *logical
id* (`pid <LogicalId>`), which is the same in every environment. This block
is generated from the stack's template by
[`scripts/resource_commands.py`](../../scripts/resource_commands.py) - see
[`../../REQUIREMENTS.md`, section 5.8](../../REQUIREMENTS.md#58---listing-every-resource-a-stack-created);
`make cdk-resources STACK=ElastiCacheStack` runs the same commands for you.

```bash
# Match these to your deployment: CDK_PRODUCT and CDK_ENVIRONMENT in .env, and
# the region you deployed to (floci: the one in .env).
PRODUCT=learning-ecs ENV=dev REGION=us-east-1
STACK=ElastiCacheStack
# Physical id of one of the stack's resources, by its logical id - the same in
# every environment. A second argument names another (e.g. nested) stack.
pid() { aws cloudformation describe-stack-resource --stack-name "${2:-$STACK}" \
  --logical-resource-id "$1" --region "$REGION" \
  --query StackResourceDetail.PhysicalResourceId --output text; }

# Every resource the stack created - type, logical id, physical id, status:
aws cloudformation describe-stack-resources --stack-name "$STACK" --region "$REGION" \
  --query "StackResources[].[ResourceType,LogicalResourceId,PhysicalResourceId,ResourceStatus]" \
  --output table

# AWS::EC2::VPC (Vpc8378EB38)
aws ec2 describe-vpcs --vpc-ids "$(pid Vpc8378EB38)" --query "Vpcs[].[VpcId,CidrBlock,State]" --output table --region "$REGION"
# AWS::ECS::Cluster (ClusterEB0386A7)
aws ecs describe-clusters --clusters "${PRODUCT}-${ENV}-ecs-elasticache" --query "clusters[].[clusterName,status]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (CacheSecurityGroupC6A3ACCF)
aws ec2 describe-security-groups --group-ids "$(pid CacheSecurityGroupC6A3ACCF)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::IAM::Role (CounterTaskDefinitionTaskRole32AD3F3C)
aws iam get-role --role-name "$(pid CounterTaskDefinitionTaskRole32AD3F3C)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (CounterTaskDefinition665BC778)
aws ecs describe-task-definition --task-definition "$(pid CounterTaskDefinition665BC778)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (CounterTaskDefinitionExecutionRole1C72326C)
aws iam get-role --role-name "$(pid CounterTaskDefinitionExecutionRole1C72326C)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (CounterLogGroupBD2D8272)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/elasticache-counter" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ECS::Service (CounterServiceE1CB42D0)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-elasticache" --services "${PRODUCT}-${ENV}-elasticache-counter" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (CounterServiceSecurityGroupBACDC71F)
aws ec2 describe-security-groups --group-ids "$(pid CounterServiceSecurityGroupBACDC71F)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::IAM::Role (ClientTaskDefinitionTaskRole3D4CF998)
aws iam get-role --role-name "$(pid ClientTaskDefinitionTaskRole3D4CF998)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (ClientTaskDefinition0504FE38)
aws ecs describe-task-definition --task-definition "$(pid ClientTaskDefinition0504FE38)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (ClientTaskDefinitionExecutionRole6FEF8DC3)
aws iam get-role --role-name "$(pid ClientTaskDefinitionExecutionRole6FEF8DC3)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (ClientLogGroup94520E53)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/elasticache-client" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (ClientSecurityGroupE3B38CB0)
aws ec2 describe-security-groups --group-ids "$(pid ClientSecurityGroupE3B38CB0)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# Also created - listed in the table above:
#   15 VPC sub-resources (subnets, route tables, gateways, endpoints) - built by shared/network.py, listed one by one in modules/01_network/README.md
#   AWS::ECS::ClusterCapacityProviderAssociations Cluster3DA9CCBA - shown by its ECS cluster (describe-clusters --include ATTACHMENTS)
#   AWS::IAM::Policy CounterTaskDefinitionTaskRoleDefaultPolicy637B97B3 - shown by its IAM role
#   AWS::IAM::Policy CounterTaskDefinitionExecutionRoleDefaultPolicy2E08DA52 - shown by its IAM role
#   AWS::IAM::Policy ClientTaskDefinitionExecutionRoleDefaultPolicy7766FC0C - shown by its IAM role
```
<!-- END resource-commands -->

## Manage it with the AWS CLI

```bash
RG=learning-ecs-dev-valkey

# backups (works on floci)
aws elasticache modify-replication-group --replication-group-id "$RG" \
  --snapshot-retention-limit 3 --snapshot-window 05:00-06:00 --apply-immediately

# scale up / out (real AWS)
aws elasticache modify-replication-group --replication-group-id "$RG" --cache-node-type cache.t4g.small --apply-immediately
aws elasticache increase-replica-count --replication-group-id "$RG" --new-replica-count 2 --apply-immediately
aws elasticache decrease-replica-count --replication-group-id "$RG" --new-replica-count 1 --apply-immediately

# test a failover: promotes a replica of node group 0001 (real AWS)
aws elasticache test-failover --replication-group-id "$RG" --node-group-id 0001
aws elasticache describe-events --source-type replication-group --source-identifier "$RG" --duration 60

# manual snapshot (real AWS)
aws elasticache create-snapshot --replication-group-id "$RG" --snapshot-name "$RG-manual-1"
aws elasticache delete-snapshot --snapshot-name "$RG-manual-1"

# delete (only the one created with the CLI on floci - the stack's is deleted by cdk destroy)
aws elasticache delete-replication-group --replication-group-id "$RG"
```

A failover changes which node the **primary endpoint** points to; clients
using the endpoint (not a node address) follow it after reconnecting -
`valkey-cli` here reconnects on every call.

## Metrics to watch

`AWS/ElastiCache`, 60-second datapoints, per node - dimensions
`CacheClusterId` (a member, e.g. `learning-ecs-dev-valkey-001`) and
`CacheNodeId` (`0001`). AWS recommends alarms on these:

| Metric | Guidance (from AWS's "Which metrics should I monitor?") |
|---|---|
| `CPUUtilization` / `EngineCPUUtilization` | Valkey is single-threaded: on a 2-vCPU node, 90% of one core is 45% `CPUUtilization`; on 4+ vCPU nodes use `EngineCPUUtilization`. Read-heavy: add replicas; write-heavy: scale up (or add shards in cluster mode) |
| `FreeableMemory` / `SwapUsage` | `FreeableMemory` below ~100 MB, or `SwapUsage` above `FreeableMemory`, means memory pressure |
| `DatabaseMemoryUsagePercentage`, `Evictions` | evictions mean the dataset doesn't fit - keys are being dropped |
| `CurrConnections` | a steady climb usually means the application leaks connections |
| `ReplicationLag` | how far behind the replica (and so the reader endpoint) is |
| `SuccessfulReadRequestLatency` / `SuccessfulWriteRequestLatency` | server-side latency |
| `TrafficManagementActive` | 1 = the node is throttling incoming commands - under-scaled |
| `CacheHits` / `CacheMisses` | effectiveness of a cache-aside pattern |

```bash
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name CurrConnections \
  --dimensions Name=CacheClusterId,Value=learning-ecs-dev-valkey-001 Name=CacheNodeId,Value=0001 \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Maximum --output table
```

## Troubleshooting

| Symptom | Where to look | Typical cause / fix |
|---|---|---|
| `valkey-cli` times out connecting | security groups | the task's security group isn't one the cache allows |
| The connection is closed or reset right away | TLS settings | the cache has encryption in transit on and the client didn't use `--tls` (or the opposite) |
| A TLS certificate verification error | `--cacert` | the client can't find a CA bundle that trusts the cache's certificate |
| `READONLY You can't write against a read only replica` | which endpoint | writes sent to the reader endpoint (or to a node address that became a replica after a failover) |
| Reads return older values than just written | `ReplicationLag` | replicas are asynchronous - read-your-writes needs the primary |
| Keys disappear | `Evictions`, `DatabaseMemoryUsagePercentage` | memory full; check the `maxmemory-policy` parameter and the node size |

More: [`../../docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md).

## floci vs real AWS

| Behavior | floci 2.1.0 | Real AWS |
|---|---|---|
| `AWS::ElastiCache::ReplicationGroup`/`SubnetGroup` from CloudFormation | **not created** (stubbed) - use `CDK_CACHE_ENDPOINT` with a CLI-created cache | created |
| `create-replication-group` (CLI) | a real Valkey container; endpoint `floci:6379` (published on your machine's 6379-6399 range) | managed nodes |
| Replicas, Multi-AZ, automatic failover | members listed, but one container; `AutomaticFailover`/`MultiAZ` stay `disabled` | as configured |
| TLS in transit | not offered on the floci endpoint (`CDK_CACHE_TLS=false`) | on by default here |
| `modify-replication-group` | snapshot settings applied; node type not changed | applied |
| `increase-replica-count`, `test-failover`, `create-snapshot` | `UnsupportedOperation` | supported |
| Metrics | not produced | produced |

## Clean up

```bash
uv run cdk destroy ElastiCacheStack
uv run python scripts/floci_prune.py --apply   # floci only
aws elasticache delete-replication-group --replication-group-id learning-ecs-dev-valkey   # floci: the CLI-created cache
```

## Notes and cautions

- **Cost**: see [Deploy to real AWS](#deploy-to-real-aws-optional).
- **No authentication beyond the network** here: access is limited by
  security groups and TLS. ElastiCache also supports AUTH tokens and
  role-based access control (users and user groups) - see
  [Authenticating users with Role-Based Access Control](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Clusters.RBAC.html).
- **Cluster mode** (sharding across node groups) scales writes beyond one
  primary; clients must be cluster-aware. **ElastiCache Serverless**
  (`aws_elasticache.CfnServerlessCache`) removes node sizing entirely.
- **A cache is not a database**: plan for an empty cache (restart,
  failover, eviction) - the counter here would simply start over.

## References

- [What is Amazon ElastiCache?](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/WhatIs.html)
- [Minimizing downtime in ElastiCache by using Multi-AZ](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/AutoFailover.html)
- [ElastiCache in-transit encryption (TLS)](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/in-transit-encryption.html)
- [Finding connection endpoints in ElastiCache](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Endpoints.html)
- [Which metrics should I monitor?](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/CacheMetrics.WhichShouldIMonitor.html) · [Monitoring CloudWatch cluster and node metrics](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/CloudWatchMetrics.html)
- [AWS::ElastiCache::ReplicationGroup](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-elasticache-replicationgroup.html)
- [Amazon ElastiCache pricing](https://aws.amazon.com/elasticache/pricing/)
- [AWS CLI Command Reference - `elasticache`](https://docs.aws.amazon.com/cli/latest/reference/elasticache/)
- [Docker Hub - valkey/valkey](https://hub.docker.com/r/valkey/valkey)
