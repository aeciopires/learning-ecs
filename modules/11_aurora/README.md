<!-- TOC -->

- [Module 11 - Aurora (multi-AZ cluster, read/write split across two services)](#module-11---aurora-multi-az-cluster-readwrite-split-across-two-services)
  - [Overview](#overview)
  - [What you will learn](#what-you-will-learn)
  - [Architecture](#architecture)
  - [RDS instance or Aurora cluster?](#rds-instance-or-aurora-cluster)
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

# Module 11 - Aurora (multi-AZ cluster, read/write split across two services)

## Overview

**Amazon Aurora** separates compute from storage: one **writer** instance
and up to 15 **readers** share a cluster volume replicated across three
Availability Zones. Two endpoints matter to an application:

- the **cluster endpoint**, which always points at the current writer
  (and follows it after a failover);
- the **reader endpoint**, which balances connections across the readers.

This module creates an Aurora cluster (Aurora PostgreSQL by default,
Aurora MySQL with a switch; **Serverless v2** instances by default,
provisioned with a switch) and shows the pattern that lets reads scale out
with readers: **two ECS services**, `api-write` on the cluster endpoint and
`api-read` on the reader endpoint (both PostgREST, [module 10](../10_rds_postgresql/README.md)),
with the **ALB routing by HTTP method** - `GET`/`HEAD` to `api-read`,
everything else to `api-write`.

## What you will learn

- `rds.DatabaseCluster` with a writer and readers (`ClusterInstance.serverless_v2` /
  `.provisioned`), promotion tiers, Serverless v2 capacity (ACUs).
- Cluster vs reader endpoints, and failover.
- An ALB rule on `http-request-method` to split reads and writes between
  two services - each scaled independently.
- Aurora's own metrics (`AuroraReplicaLag`, `ServerlessDatabaseCapacity`,
  `ACUUtilization`, ...).

## Architecture

```
 internet -> ALB learning-ecs-dev-aurora-alb
               rule 10: method GET|HEAD -> api-read tasks  -- PGRST_DB_URI=...@<reader endpoint>
               default (POST, PATCH...) -> api-write tasks -- PGRST_DB_URI=...@<cluster endpoint>
                                                  |
             Aurora PostgreSQL cluster learning-ecs-dev-aurora-postgresql  (isolated subnets)
               writer (promotion tier 0)   reader1 (tier 1, scales with the writer)   [reader2...]
               \_________________ one cluster volume, replicated across 3 AZs ________________/

 aws ecs run-task (client: postgres image) -> migration on the cluster endpoint
 CDK_AURORA_ENGINE=mysql: Aurora MySQL + WordPress on the cluster endpoint (no split)
```

## RDS instance or Aurora cluster?

| | RDS instance ([09](../09_rds_mysql/README.md), [10](../10_rds_postgresql/README.md)) | Aurora cluster (this module) |
|---|---|---|
| High availability | Multi-AZ standby (not readable) | readers in other AZs, promoted on failover |
| Read scaling | read replicas (separate endpoints) | up to 15 readers behind one reader endpoint |
| Storage | EBS volume you size (autoscaling up to a limit) | cluster volume that grows automatically |
| Serverless capacity | no | Serverless v2 (ACUs, scales per second) |
| Cost floor | lower | higher (Serverless v2 bills per ACU-hour; minimum capacity applies) |

## AWS services and CDK constructs used

| AWS service | CDK construct (Python) | Level |
|---|---|---|
| Amazon Aurora | `aws_cdk.aws_rds.DatabaseCluster`, `ClusterInstance.serverless_v2` / `.provisioned`, `DatabaseClusterEngine.aurora_postgres` / `.aurora_mysql` | L2 |
| Elastic Load Balancing | `ListenerCondition.http_request_methods` | L2 |
| Amazon ECS | two `FargateService`s (PostgREST) or one (WordPress), one client `FargateTaskDefinition` | L2 |
| AWS Secrets Manager | `Secret` (master, and the `authenticator` app user for PostgreSQL) | L2 |

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CDK_AURORA_ENGINE` | `postgresql` | `postgresql` (PostgREST read/write split) or `mysql` (WordPress) |
| `CDK_AURORA_POSTGRES_VERSION` / `CDK_AURORA_MYSQL_VERSION` | `17.9` / `8.0.mysql_aurora.3.12.0` | engine versions |
| `CDK_AURORA_INSTANCES` | `serverless` | `serverless` (Serverless v2) or `provisioned` |
| `CDK_AURORA_INSTANCE_TYPE` | `t4g.medium` | class of provisioned instances (no `db.` prefix) |
| `CDK_AURORA_MIN_ACU` / `CDK_AURORA_MAX_ACU` | `0.5` / `4` | Serverless v2 capacity range |
| `CDK_AURORA_READERS` | `1` | number of readers (`0` = writer only, no HA) |
| `CDK_AURORA_BACKUP_DAYS` | `1` | backup retention |
| `CDK_AURORA_PERFORMANCE_INSIGHTS` | `false` | Performance Insights |
| `CDK_POSTGREST_LOG_LEVEL` | `info` | PostgREST log level (`info` logs every request - see who served what) |
| `CDK_AURORA_DESIRED_COUNT` | `CDK_DESIRED_COUNT` (`2`) | tasks per service |
| `CDK_PORT_AURORA` | `80` (`.env.example`: `8090`) | ALB listener port |

Promotion tiers decide which reader becomes the writer on failover
(lowest tier first). Provisioned readers 1-2 go in tier 1, the next two in
tier 2, and so on. For Serverless v2 the CDK exposes this as
`scale_with_writer`: readers 1-2 get `scale_with_writer=True` (tier 1) and
the rest tier 2. Serverless v2 readers in tiers 0-1 "scale alongside the
writer even if the current read load does not require the capacity", so the
one promoted on failover is already sized for the writer's load (CDK
`aws_rds` README, "Capacity & Scaling").

## Prerequisites

[Module 10](../10_rds_postgresql/README.md) (PostgREST, migrations).
floci running and `.env` loaded.

## Tests

[`../../tests/unit/test_11_aurora.py`](../../tests/unit/test_11_aurora.py)
checks the default Aurora PostgreSQL Serverless v2 cluster with one reader,
configurable readers/provisioned class/promotion tiers, the GET/HEAD rule to
the reader service, that each API uses the right endpoint, the Aurora MySQL
+ WordPress variant, invalid choices, and the mandatory tags:

```bash
uv run pytest tests/unit/test_11_aurora.py -v
```

## Deploy with floci (local, free)

```bash
uv run cdk bootstrap
uv run cdk synth AuroraStack
uv run cdk diff AuroraStack
uv run cdk deploy AuroraStack --require-approval never --method=direct
# Changing the stack later on floci: destroy first, then deploy (REQUIREMENTS.md section 5.6).
```

## Deploy to real AWS (optional)

**Aurora bills per instance-hour (or per ACU-hour for Serverless v2, from
the minimum capacity up), plus storage and I/O** - see
[Amazon Aurora pricing](https://aws.amazon.com/rds/aurora/pricing/). With
the defaults: two Serverless v2 instances at 0.5-4 ACU each.

```bash
unset AWS_ENDPOINT_URL CDK_PORT_AURORA
uv run cdk bootstrap --profile <your-aws-cli-profile>
uv run cdk diff AuroraStack --profile <your-aws-cli-profile>
uv run cdk deploy AuroraStack --profile <your-aws-cli-profile>
```

## Verify

```bash
out() { aws cloudformation describe-stacks --stack-name AuroraStack \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
aws rds describe-db-clusters --db-cluster-identifier learning-ecs-dev-aurora-postgresql \
  --query 'DBClusters[0].{status:Status,writer:Endpoint,reader:ReaderEndpoint,members:DBClusterMembers[].[DBInstanceIdentifier,IsClusterWriter,PromotionTier],acu:ServerlessV2ScalingConfiguration}'

# 1. the migration (same client task as module 10, run against the cluster endpoint)
CLUSTER=$(out ClusterName); SG=$(out ClientSecurityGroupId); TD=$(out ClientTaskDefinitionArn)
SUBNETS=$(aws ecs describe-services --cluster "$CLUSTER" --services learning-ecs-dev-aurora-api-write \
  --query 'services[0].networkConfiguration.awsvpcConfiguration.subnets' --output text | tr '\t' ',')
TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$TD" \
  --capacity-provider-strategy capacityProvider=FARGATE,weight=1 \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"

# 2. a write and a read through the ALB
URL=$(out Url); [ -n "${AWS_ENDPOINT_URL:-}" ] && URL=http://localhost:${CDK_PORT_AURORA:-8090}/
curl -s -o /dev/null -w 'POST %{http_code}\n' -X POST "${URL}todos" -H 'Content-Type: application/json' \
  -d '{"task":"written via api-write"}'
curl -s "${URL}todos"

# 3. who served what (PostgREST logs every request at log level "info")
aws logs tail /ecs/learning-ecs/dev/aurora-api-write --since 5m | grep '/todos'    # real AWS: the POST
aws logs tail /ecs/learning-ecs/dev/aurora-api-read  --since 5m | grep '/todos'    # real AWS: the GET
docker logs learning-ecs-floci 2>&1 | grep -E 'ecs:learning-ecs-dev-aurora-api' | grep '/todos' | tail -3   # floci
# api-write ... "POST /todos HTTP/1.1" 201 ...   /   api-read ... "GET /todos HTTP/1.1" 200 ...
```

The client task's ARN comes from the stack output (`ClientTaskDefinitionArn`):
after a destroy and redeploy, floci keeps older revisions of the family
active and doesn't honour `list-task-definitions --sort DESC`, so "the
newest revision" could be a stale one pointing at a deleted secret.

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
`make cdk-resources STACK=AuroraStack` runs the same commands for you.

```bash
# Match these to your deployment: CDK_PRODUCT and CDK_ENVIRONMENT in .env, and
# the region you deployed to (floci: the one in .env).
PRODUCT=learning-ecs ENV=dev REGION=us-east-1
STACK=AuroraStack
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
aws ecs describe-clusters --clusters "${PRODUCT}-${ENV}-ecs-aurora" --query "clusters[].[clusterName,status]" --output table --region "$REGION"
# AWS::SecretsManager::Secret (MasterSecretA11BF785)
aws secretsmanager describe-secret --secret-id "${PRODUCT}-${ENV}-secret-aurora" --query "[Name,ARN]" --output table --region "$REGION"
# AWS::RDS::DBSubnetGroup (DatabaseSubnets56F17B9A)
aws rds describe-db-subnet-groups --db-subnet-group-name "$(pid DatabaseSubnets56F17B9A)" --query "DBSubnetGroups[].DBSubnetGroupName" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (DatabaseSecurityGroup5C91FDCB)
aws ec2 describe-security-groups --group-ids "$(pid DatabaseSecurityGroup5C91FDCB)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::RDS::DBCluster (DatabaseB269D8BB)
aws rds describe-db-clusters --db-cluster-identifier "${PRODUCT}-${ENV}-aurora-postgresql" --query "DBClusters[].[DBClusterIdentifier,Engine,Status]" --output table --region "$REGION"
# AWS::RDS::DBInstance (Databasewriter2462CC03)
aws rds describe-db-instances --db-instance-identifier "$(pid Databasewriter2462CC03)" --query "DBInstances[].[DBInstanceIdentifier,Engine,DBInstanceStatus]" --output table --region "$REGION"
# AWS::RDS::DBInstance (Databasereader1F54479B8)
aws rds describe-db-instances --db-instance-identifier "$(pid Databasereader1F54479B8)" --query "DBInstances[].[DBInstanceIdentifier,Engine,DBInstanceStatus]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::LoadBalancer (Alb16C2F182)
aws elbv2 describe-load-balancers --load-balancer-arns "$(pid Alb16C2F182)" --query "LoadBalancers[].[LoadBalancerName,Type,State.Code]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (AlbSecurityGroup580F65A6)
aws ec2 describe-security-groups --group-ids "$(pid AlbSecurityGroup580F65A6)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::Listener (AlbHttp7966E42E)
aws elbv2 describe-listeners --listener-arns "$(pid AlbHttp7966E42E)" --query "Listeners[].[Port,Protocol]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::TargetGroup (AlbHttpWriteGroup9CD06CA6)
aws elbv2 describe-target-groups --target-group-arns "$(pid AlbHttpWriteGroup9CD06CA6)" --query "TargetGroups[].[TargetGroupName,Port,TargetType]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::TargetGroup (AlbHttpReadGroupB75C4936)
aws elbv2 describe-target-groups --target-group-arns "$(pid AlbHttpReadGroupB75C4936)" --query "TargetGroups[].[TargetGroupName,Port,TargetType]" --output table --region "$REGION"
# AWS::SecretsManager::Secret (AppSecretFAB5164C)
aws secretsmanager describe-secret --secret-id "${PRODUCT}-${ENV}-secret-aurora-app" --query "[Name,ARN]" --output table --region "$REGION"
# AWS::IAM::Role (ApiWriteTaskDefinitionTaskRole8A463F39)
aws iam get-role --role-name "$(pid ApiWriteTaskDefinitionTaskRole8A463F39)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (ApiWriteTaskDefinitionA7A238BA)
aws ecs describe-task-definition --task-definition "$(pid ApiWriteTaskDefinitionA7A238BA)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (ApiWriteTaskDefinitionExecutionRoleBD5D924E)
aws iam get-role --role-name "$(pid ApiWriteTaskDefinitionExecutionRoleBD5D924E)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (ApiWriteLogGroup3F50980E)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/aurora-api-write" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ECS::Service (ApiWriteServiceBB470C68)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-aurora" --services "${PRODUCT}-${ENV}-aurora-api-write" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (ApiWriteServiceSecurityGroupDD5F3B4F)
aws ec2 describe-security-groups --group-ids "$(pid ApiWriteServiceSecurityGroupDD5F3B4F)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::IAM::Role (ApiReadTaskDefinitionTaskRole91EC595E)
aws iam get-role --role-name "$(pid ApiReadTaskDefinitionTaskRole91EC595E)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (ApiReadTaskDefinitionF8389771)
aws ecs describe-task-definition --task-definition "$(pid ApiReadTaskDefinitionF8389771)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (ApiReadTaskDefinitionExecutionRoleF2D36761)
aws iam get-role --role-name "$(pid ApiReadTaskDefinitionExecutionRoleF2D36761)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (ApiReadLogGroup8ACB7DB9)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/aurora-api-read" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ECS::Service (ApiReadService2C69CB38)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-aurora" --services "${PRODUCT}-${ENV}-aurora-api-read" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (ApiReadServiceSecurityGroup0F1E2A8E)
aws ec2 describe-security-groups --group-ids "$(pid ApiReadServiceSecurityGroup0F1E2A8E)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::IAM::Role (ClientTaskDefinitionTaskRole3D4CF998)
aws iam get-role --role-name "$(pid ClientTaskDefinitionTaskRole3D4CF998)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (ClientTaskDefinition0504FE38)
aws ecs describe-task-definition --task-definition "$(pid ClientTaskDefinition0504FE38)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (ClientTaskDefinitionExecutionRole6FEF8DC3)
aws iam get-role --role-name "$(pid ClientTaskDefinitionExecutionRole6FEF8DC3)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (ClientLogGroup94520E53)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/aurora-client" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (ClientSecurityGroupE3B38CB0)
aws ec2 describe-security-groups --group-ids "$(pid ClientSecurityGroupE3B38CB0)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# Also created - listed in the table above:
#   15 VPC sub-resources (subnets, route tables, gateways, endpoints) - built by shared/network.py, listed one by one in modules/01_network/README.md
#   AWS::ECS::ClusterCapacityProviderAssociations Cluster3DA9CCBA - shown by its ECS cluster (describe-clusters --include ATTACHMENTS)
#   AWS::EC2::SecurityGroupIngress DatabaseSecurityGroupfromAuroraStackApiWriteServiceSecurityGroup56ABF834IndirectPort01B9BD96 - shown by its security group
#   AWS::EC2::SecurityGroupIngress DatabaseSecurityGroupfromAuroraStackApiReadServiceSecurityGroup5C105113IndirectPort54B62312 - shown by its security group
#   AWS::EC2::SecurityGroupIngress DatabaseSecurityGroupfromAuroraStackClientSecurityGroupD4A4BC71IndirectPortDA2FB89D - shown by its security group
#   AWS::EC2::SecurityGroupEgress AlbSecurityGrouptoAuroraStackApiWriteServiceSecurityGroup56ABF8343001B3415010 - shown by its security group
#   AWS::EC2::SecurityGroupEgress AlbSecurityGrouptoAuroraStackApiReadServiceSecurityGroup5C10511330016FA854E3 - shown by its security group
#   AWS::EC2::SecurityGroupEgress AlbSecurityGrouptoAuroraStackApiWriteServiceSecurityGroup56ABF8343000E84A9779 - shown by its security group
#   AWS::EC2::SecurityGroupEgress AlbSecurityGrouptoAuroraStackApiReadServiceSecurityGroup5C10511330001D27D557 - shown by its security group
#   AWS::ElasticLoadBalancingV2::ListenerRule AlbHttpReadRuleC8861C89 - shown by its listener (elbv2 describe-rules)
#   AWS::IAM::Policy ApiWriteTaskDefinitionTaskRoleDefaultPolicyED0F4A0D - shown by its IAM role
#   AWS::IAM::Policy ApiWriteTaskDefinitionExecutionRoleDefaultPolicy5E3EEB2A - shown by its IAM role
#   AWS::EC2::SecurityGroupIngress ApiWriteServiceSecurityGroupfromAuroraStackAlbSecurityGroup4E0B7E143001CC6F0245 - shown by its security group
#   AWS::EC2::SecurityGroupIngress ApiWriteServiceSecurityGroupfromAuroraStackAlbSecurityGroup4E0B7E14300067C3C6B7 - shown by its security group
#   AWS::IAM::Policy ApiReadTaskDefinitionTaskRoleDefaultPolicy5C4B268F - shown by its IAM role
#   AWS::IAM::Policy ApiReadTaskDefinitionExecutionRoleDefaultPolicyAF01DA7E - shown by its IAM role
#   AWS::EC2::SecurityGroupIngress ApiReadServiceSecurityGroupfromAuroraStackAlbSecurityGroup4E0B7E143001484B4704 - shown by its security group
#   AWS::EC2::SecurityGroupIngress ApiReadServiceSecurityGroupfromAuroraStackAlbSecurityGroup4E0B7E1430004DC67108 - shown by its security group
#   AWS::IAM::Policy ClientTaskDefinitionExecutionRoleDefaultPolicy7766FC0C - shown by its IAM role
```
<!-- END resource-commands -->

## Manage it with the AWS CLI

```bash
C=learning-ecs-dev-aurora-postgresql

# capacity range of the Serverless v2 instances (works on floci)
aws rds modify-db-cluster --db-cluster-identifier "$C" \
  --serverless-v2-scaling-configuration MinCapacity=0.5,MaxCapacity=8 --apply-immediately

# add / remove a reader (works on floci)
aws rds create-db-instance --db-instance-identifier "$C-reader2" --db-cluster-identifier "$C" \
  --engine aurora-postgresql --db-instance-class db.serverless --promotion-tier 2
aws rds delete-db-instance --db-instance-identifier "$C-reader2"

# fail over to a reader (real AWS; floci: UnsupportedOperation) - the cluster endpoint follows the new writer
aws rds failover-db-cluster --db-cluster-identifier "$C"
aws rds describe-db-clusters --db-cluster-identifier "$C" \
  --query 'DBClusters[0].DBClusterMembers[].[DBInstanceIdentifier,IsClusterWriter]'

# endpoints, snapshots (real AWS; floci: UnsupportedOperation / not modeled)
aws rds describe-db-cluster-endpoints --db-cluster-identifier "$C" --query 'DBClusterEndpoints[].[EndpointType,Endpoint]'
aws rds create-db-cluster-snapshot --db-cluster-identifier "$C" --db-cluster-snapshot-identifier "$C-manual-1"
aws rds delete-db-cluster-snapshot --db-cluster-snapshot-identifier "$C-manual-1"
```

Scale the two API services independently (module 16 automates it):

```bash
aws ecs update-service --cluster learning-ecs-dev-ecs-aurora --service learning-ecs-dev-aurora-api-read --desired-count 4
```

## Metrics to watch

`AWS/RDS`, cluster-level dimension `DBClusterIdentifier=learning-ecs-dev-aurora-postgresql`
(instance-level: `DBInstanceIdentifier`):

| Metric | Unit | Notes |
|---|---|---|
| `AuroraReplicaLag` (per reader) / `AuroraReplicaLagMaximum` | Milliseconds | how stale `api-read` can be |
| `ServerlessDatabaseCapacity` | ACUs | current capacity |
| `ACUUtilization` | Percent | capacity / max ACU - near 100% means raise `CDK_AURORA_MAX_ACU` |
| `CPUUtilization`, `FreeableMemory`, `DatabaseConnections` | | per instance - compare writer and readers |
| `CommitLatency`, `CommitThroughput`, `Deadlocks`, `BufferCacheHitRatio` | | engine health |
| `VolumeBytesUsed`, `VolumeReadIOPs`, `VolumeWriteIOPs` | | storage and I/O billing |

```bash
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name AuroraReplicaLagMaximum \
  --dimensions Name=DBClusterIdentifier,Value=learning-ecs-dev-aurora-postgresql \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Maximum --output table
```

## Troubleshooting

| Symptom | Where to look | Typical cause / fix |
|---|---|---|
| A write through `api-read` fails with a read-only transaction error (real AWS) | the routing rule | readers are read-only - writes must go to the cluster endpoint (here: any method but GET/HEAD) |
| Read right after write doesn't see the row | `AuroraReplicaLag` | reads from readers are eventually consistent (usually milliseconds) - read-your-writes needs the writer |
| `api-write` errors for a short time, then recovers | RDS events (`describe-events --source-type db-cluster`) | a failover: the cluster endpoint moved to the new writer and existing connections were reset - PostgREST reconnects on its own |
| Serverless v2 slow to respond after idle | `ServerlessDatabaseCapacity` | scaling up from a low minimum takes time - raise `CDK_AURORA_MIN_ACU` |
| PostgREST: "Backend database authentication failed" (floci) / password authentication failed | migration, secrets | the migration hasn't run against *this* cluster, or tasks were started with an old task definition revision - use the stack's `ClientTaskDefinitionArn` |

More: [`../../docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md).

## floci vs real AWS

| Behavior | floci 2.1.0 | Real AWS |
|---|---|---|
| Cluster + instances from CloudFormation | created; **one PostgreSQL/MySQL container serves the whole cluster** | writer + readers on separate compute |
| Cluster and reader endpoints | the same address (floci's IP + a proxy port) | two DNS names |
| Readers are read-only | **no** (same database) - writes via `api-read` would succeed | yes |
| `IsClusterWriter` | `true` for every member | one writer |
| Serverless v2 scaling configuration, adding/removing instances | stored / works; capacity doesn't actually scale | scales |
| `failover-db-cluster`, `describe-db-cluster-endpoints`, cluster snapshots | `UnsupportedOperation` / not modeled | supported |
| Changed stack re-deployed in place | update failed while writing this module - destroy and deploy | in-place update |

The ALB's method-based split and the two services are real on floci; what
it can't show is the database-side difference between writer and readers.

## Clean up

```bash
uv run cdk destroy AuroraStack
uv run python scripts/floci_prune.py --apply   # floci only
```

## Notes and cautions

- **Cost**: see [Deploy to real AWS](#deploy-to-real-aws-optional).
- **Splitting by HTTP method is a convention, not a guarantee**: a `GET`
  that writes (bad API design) would fail on the reader; an RPC call via
  `POST` that only reads goes to the writer. It's simple and it works for
  REST APIs that respect HTTP semantics.
- **Aurora Global Database** extends this across regions (a primary region
  and up to several read-only secondary regions) - see
  [module 18](../18_multi_region/README.md) and
  [Using Amazon Aurora Global Database](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-global-database.html).

## References

- [Amazon Aurora DB clusters](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Overview.html) · [Amazon Aurora endpoint connections](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Overview.Endpoints.html)
- [High availability for Amazon Aurora](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Concepts.AuroraHighAvailability.html)
- [Using Aurora Serverless v2](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.html) · [Performance and scaling for Aurora Serverless v2](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2.setting-capacity.html)
- [Amazon CloudWatch metrics for Amazon Aurora](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.AuroraMonitoring.Metrics.html)
- [Listener rule condition types (http-request-method)](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/rule-condition-types.html)
- [Amazon Aurora pricing](https://aws.amazon.com/rds/aurora/pricing/)
- [AWS CDK API Reference (Python) - `aws_cdk.aws_rds.DatabaseCluster`](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/DatabaseCluster.html)
