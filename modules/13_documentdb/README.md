<!-- TOC -->

- [Module 13 - Amazon DocumentDB read and written by ECS tasks (TLS, init container)](#module-13---amazon-documentdb-read-and-written-by-ecs-tasks-tls-init-container)
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

# Module 13 - Amazon DocumentDB read and written by ECS tasks (TLS, init container)

## Overview

**Amazon DocumentDB** is AWS's managed, MongoDB-compatible document
database: a primary instance plus replicas over a shared cluster volume,
with **TLS required by default**. This module runs two ECS consumers
against it, both built only from Docker Hub images:

- **`events`**, a service whose tasks insert a document into
  `app.events` and count the collection every 15 seconds, using `mongosh`
  from the official [`mongo`](https://hub.docker.com/_/mongo) image;
- **`*-documentdb-client`**, a one-off task definition that runs any
  JavaScript you pass.

The interesting ECS part is how TLS works: DocumentDB's certificate is
issued by Amazon's CA, whose bundle the `mongo` image doesn't contain - and
the image has no `curl` or `wget` to fetch it. So every task first runs an
**init container** ([`curlimages/curl`](https://hub.docker.com/r/curlimages/curl))
that downloads the bundle into a **task volume**, and the `mongosh`
container **depends on it finishing with `SUCCESS`** before it starts.

## What you will learn

- `aws_docdb.DatabaseCluster` (primary + replicas in other AZs), the
  `tls` cluster parameter, and the connection flags DocumentDB documents
  (`--tls --tlsCAFile global-bundle.pem --retryWrites false`).
- ECS **container dependencies** (`dependsOn`, condition `SUCCESS`),
  non-essential containers, and a **task-scoped volume** shared between
  two containers.
- Running a container as a different user (`user="0"`) when the volume's
  owner requires it.
- "Bring your own cluster" with an existing secret.

## Architecture

```
 events task (service, x2)                         client task (aws ecs run-task)
 +---------------------------------------------+   (same two containers)
 | ca-bundle (curlimages/curl, essential=false) |
 |   curl -o /certs/global-bundle.pem ... -> exit 0
 |                    | dependsOn: SUCCESS       |
 | mongosh (mongo:8.0)  v                        |
 |   mongosh --tls --tlsCAFile /certs/... --eval "insertOne + countDocuments"
 |   volume "certs" (task-scoped): rw for ca-bundle, ro for mongosh
 +----------------------|-----------------------+
                        | 27017, only from the two task SGs
                        v
   DocumentDB cluster learning-ecs-dev-docdb  (isolated subnets)
     primary (AZ a)   replica (AZ b)   -- shared cluster volume --
```

## AWS services and CDK constructs used

| AWS service | CDK construct (Python) | Level |
|---|---|---|
| Amazon DocumentDB | `aws_cdk.aws_docdb.DatabaseCluster`, `Login`, `ClusterParameterGroup` | L2 |
| AWS Secrets Manager | `Secret` (via `shared/database.py`), or `Secret.from_secret_name_v2` (bring your own) | L2 |
| Amazon ECS | `FargateTaskDefinition.add_volume`, `ContainerDefinition.add_mount_points`, `add_container_dependencies(ContainerDependency(condition=SUCCESS))` | L2 |
| Amazon ECS | `FargateService` (events), one-off client task definition | L2 |

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CDK_DOCDB_VERSION` | `5.0.0` | engine version |
| `CDK_DOCDB_INSTANCE_TYPE` | `t4g.medium` | instance class (no `db.` prefix) |
| `CDK_DOCDB_INSTANCES` | `2` | instances (1 primary + replicas, spread across AZs) |
| `CDK_DOCDB_TLS` | `true` (`.env.example`: `false`) | `false` creates a cluster parameter group with `tls=disabled` and drops the init container |
| `CDK_DOCDB_PARAMETER_FAMILY` | `docdb5.0` | family of that parameter group (match the engine version) |
| `CDK_DOCDB_PORT` | `27017` | port |
| `CDK_DOCDB_ENDPOINT` / `CDK_DOCDB_SECRET_NAME` | unset / `<product>-<env>-secret-documentdb` | use an existing cluster and an existing `{"username","password"}` secret |
| `CDK_DOCDB_INTERVAL_SECONDS` | `15` | how often each events task writes |
| `CDK_DOCDB_DESIRED_COUNT` | `CDK_DESIRED_COUNT` (`2`) | events tasks |
| `CDK_DOCDB_CLIENT_IMAGE` | `mongo:8.0` | image with `mongosh` |

## Prerequisites

[Modules 09-12](../09_rds_mysql/README.md) (data patterns, one-off
tasks). floci running and `.env` loaded.

## Tests

[`../../tests/unit/test_13_documentdb.py`](../../tests/unit/test_13_documentdb.py)
checks the two-instance encrypted cluster and its by-name password, the
TLS task layout (non-essential root `ca-bundle` container fetching the
official bundle URL, `mongosh` depending on it with `SUCCESS`, the shared
volume mounted read-only), the no-TLS variant (no init container, a
`tls=disabled` parameter group), the password as an ECS secret, the
bring-your-own mode, the security group rules, and the mandatory tags:

```bash
uv run pytest tests/unit/test_13_documentdb.py -v
```

## Deploy with floci (local, free)

floci's CloudFormation doesn't create DocumentDB resources (stubbed - see
[`../../REQUIREMENTS.md`, section 10](../../REQUIREMENTS.md#10-floci-vs-real-aws)),
but its DocumentDB API runs a real MongoDB container. So, on floci, create
the credentials and the cluster with the CLI and point the stack at them
(`.env.example` already sets `CDK_DOCDB_TLS=false`; floci's MongoDB
doesn't offer TLS):

```bash
PASS=$(openssl rand -hex 16)
aws secretsmanager create-secret --name learning-ecs-dev-secret-documentdb \
  --secret-string "{\"username\":\"docdbadmin\",\"password\":\"$PASS\"}"
aws docdb create-db-cluster --db-cluster-identifier learning-ecs-dev-docdb --engine docdb \
  --master-username docdbadmin --master-user-password "$PASS"
aws docdb create-db-instance --db-instance-identifier learning-ecs-dev-docdb-1 \
  --db-cluster-identifier learning-ecs-dev-docdb --db-instance-class db.t4g.medium --engine docdb
export CDK_DOCDB_ENDPOINT=$(aws docdb describe-db-clusters --db-cluster-identifier learning-ecs-dev-docdb \
  --query 'DBClusters[0].Endpoint' --output text)

uv run cdk bootstrap
uv run cdk synth DocumentDbStack
uv run cdk diff DocumentDbStack
uv run cdk deploy DocumentDbStack --require-approval never --method=direct
```

## Deploy to real AWS (optional)

**Each DocumentDB instance bills per hour, plus storage and I/O** - see
[Amazon DocumentDB pricing](https://aws.amazon.com/documentdb/pricing/).
Creating the cluster takes several minutes.

```bash
unset AWS_ENDPOINT_URL CDK_DOCDB_TLS CDK_DOCDB_ENDPOINT   # REQUIREMENTS.md section 9.2
uv run cdk bootstrap --profile <your-aws-cli-profile>
uv run cdk diff DocumentDbStack --profile <your-aws-cli-profile>
uv run cdk deploy DocumentDbStack --profile <your-aws-cli-profile>
```

## Verify

```bash
aws logs tail /ecs/learning-ecs/dev/documentdb-events --follow                                     # real AWS
docker logs learning-ecs-floci 2>&1 | grep 'ecs:learning-ecs-dev-documentdb-events' | tail -4     # floci
# task=47a8e6b88b5b inserted; events=1
# task=dea329c6d5cc inserted; events=2     <- the other task, same collection
```

Read the collection with the one-off client (its default `JS` prints the
count and the three newest documents; override `JS` for anything else):

```bash
out() { aws cloudformation describe-stacks --stack-name DocumentDbStack \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
CLUSTER=$(out ClusterName); SG=$(out ClientSecurityGroupId); TD=$(out ClientTaskDefinitionArn)
SUBNETS=$(aws ecs describe-services --cluster "$CLUSTER" --services learning-ecs-dev-documentdb-events \
  --query 'services[0].networkConfiguration.awsvpcConfiguration.subnets' --output text | tr '\t' ',')
NET="awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}"
TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$TD" \
  --capacity-provider-strategy capacityProvider=FARGATE,weight=1 --network-configuration "$NET" \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[].[name,exitCode]' --output table
# real AWS: ca-bundle 0, mongosh 0      (floci, no TLS: mongosh 0 only)
aws logs tail /ecs/learning-ecs/dev/documentdb-client --since 5m                                          # real AWS
docker logs learning-ecs-floci 2>&1 | grep 'ecs:learning-ecs-dev-documentdb-client:mongosh' | tail -12   # floci

# any JavaScript, e.g. an index and a query:
aws ecs run-task --cluster "$CLUSTER" --task-definition "$TD" \
  --capacity-provider-strategy capacityProvider=FARGATE,weight=1 --network-configuration "$NET" \
  --overrides '{"containerOverrides":[{"name":"mongosh","environment":[{"name":"JS","value":"const c=db.getSiblingDB(\"app\").events; c.createIndex({at:-1}); printjson(c.find({}, {task:1}).sort({at:-1}).limit(5).toArray())"}]}]}'
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
`make cdk-resources STACK=DocumentDbStack` runs the same commands for you.

```bash
# Match these to your deployment: CDK_PRODUCT and CDK_ENVIRONMENT in .env, and
# the region you deployed to (floci: the one in .env).
PRODUCT=learning-ecs ENV=dev REGION=us-east-1
STACK=DocumentDbStack
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
aws ecs describe-clusters --clusters "${PRODUCT}-${ENV}-ecs-documentdb" --query "clusters[].[clusterName,status]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (DocDbSecurityGroup3F7E6309)
aws ec2 describe-security-groups --group-ids "$(pid DocDbSecurityGroup3F7E6309)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::SecretsManager::Secret (MasterSecretA11BF785)
aws secretsmanager describe-secret --secret-id "${PRODUCT}-${ENV}-secret-documentdb" --query "[Name,ARN]" --output table --region "$REGION"
# AWS::DocDB::DBClusterParameterGroup (NoTlsParameters6D99F68E)
aws docdb describe-db-cluster-parameter-groups --db-cluster-parameter-group-name "$(pid NoTlsParameters6D99F68E)" --query "DBClusterParameterGroups[].[DBClusterParameterGroupName,DBParameterGroupFamily]" --output table --region "$REGION"
# AWS::DocDB::DBSubnetGroup (DatabaseSubnets56F17B9A)
aws docdb describe-db-subnet-groups --db-subnet-group-name "$(pid DatabaseSubnets56F17B9A)" --query "DBSubnetGroups[].DBSubnetGroupName" --output table --region "$REGION"
# AWS::DocDB::DBCluster (DatabaseB269D8BB)
aws docdb describe-db-clusters --db-cluster-identifier "${PRODUCT}-${ENV}-docdb" --query "DBClusters[].[DBClusterIdentifier,Status,EngineVersion]" --output table --region "$REGION"
# AWS::DocDB::DBInstance (DatabaseInstance1844F58FD)
aws docdb describe-db-instances --db-instance-identifier "${PRODUCT}-${ENV}-docdbinstance1" --query "DBInstances[].[DBInstanceIdentifier,DBInstanceClass,DBInstanceStatus]" --output table --region "$REGION"
# AWS::DocDB::DBInstance (DatabaseInstance2AA380DEE)
aws docdb describe-db-instances --db-instance-identifier "${PRODUCT}-${ENV}-docdbinstance2" --query "DBInstances[].[DBInstanceIdentifier,DBInstanceClass,DBInstanceStatus]" --output table --region "$REGION"
# AWS::IAM::Role (EventsTaskDefinitionTaskRole1C378F28)
aws iam get-role --role-name "$(pid EventsTaskDefinitionTaskRole1C378F28)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (EventsTaskDefinition6E5CFB36)
aws ecs describe-task-definition --task-definition "$(pid EventsTaskDefinition6E5CFB36)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (EventsTaskDefinitionExecutionRole033F76DE)
aws iam get-role --role-name "$(pid EventsTaskDefinitionExecutionRole033F76DE)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (EventsTaskDefinitionLogGroupCC466CA9)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/documentdb-events" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ECS::Service (EventsService26F43824)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-documentdb" --services "${PRODUCT}-${ENV}-documentdb-events" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (EventsServiceSecurityGroup08E58118)
aws ec2 describe-security-groups --group-ids "$(pid EventsServiceSecurityGroup08E58118)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::IAM::Role (ClientTaskDefinitionTaskRole3D4CF998)
aws iam get-role --role-name "$(pid ClientTaskDefinitionTaskRole3D4CF998)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (ClientTaskDefinition0504FE38)
aws ecs describe-task-definition --task-definition "$(pid ClientTaskDefinition0504FE38)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (ClientTaskDefinitionExecutionRole6FEF8DC3)
aws iam get-role --role-name "$(pid ClientTaskDefinitionExecutionRole6FEF8DC3)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (ClientTaskDefinitionLogGroup1E56504B)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/documentdb-client" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (ClientSecurityGroupE3B38CB0)
aws ec2 describe-security-groups --group-ids "$(pid ClientSecurityGroupE3B38CB0)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# Also created - listed in the table above:
#   15 VPC sub-resources (subnets, route tables, gateways, endpoints) - built by shared/network.py, listed one by one in modules/01_network/README.md
#   AWS::ECS::ClusterCapacityProviderAssociations Cluster3DA9CCBA - shown by its ECS cluster (describe-clusters --include ATTACHMENTS)
#   AWS::IAM::Policy EventsTaskDefinitionTaskRoleDefaultPolicyEAC8F081 - shown by its IAM role
#   AWS::IAM::Policy EventsTaskDefinitionExecutionRoleDefaultPolicyDC059178 - shown by its IAM role
#   AWS::IAM::Policy ClientTaskDefinitionExecutionRoleDefaultPolicy7766FC0C - shown by its IAM role
```

**On floci** (2.1.0), CloudFormation records `AWS::DocDB::DBCluster`, `AWS::DocDB::DBClusterParameterGroup`, `AWS::DocDB::DBInstance`, `AWS::DocDB::DBSubnetGroup` without creating them, so those commands find nothing there - they work on real AWS. See [`REQUIREMENTS.md`, section 10](../../REQUIREMENTS.md#10-floci-vs-real-aws).
<!-- END resource-commands -->

## Manage it with the AWS CLI

```bash
C=learning-ecs-dev-docdb   # the stack's cluster; on floci, the one you created with the CLI

# which parameter group governs the cluster, and is TLS on? (from the DocumentDB docs)
aws docdb describe-db-clusters --db-cluster-identifier "$C" \
  --query 'DBClusters[*].[DBClusterIdentifier,DBClusterParameterGroup]'
aws docdb describe-db-cluster-parameters --db-cluster-parameter-group-name <group> \
  --query "Parameters[?ParameterName=='tls'].[ParameterValue,ApplyType]"   # tls is static: needs a reboot

# backups (works on floci)
aws docdb modify-db-cluster --db-cluster-identifier "$C" --backup-retention-period 7 --apply-immediately

# add / remove a replica (works on floci)
aws docdb create-db-instance --db-instance-identifier "$C-2" --db-cluster-identifier "$C" \
  --db-instance-class db.t4g.medium --engine docdb
aws docdb delete-db-instance --db-instance-identifier "$C-2"

# fail over to a replica (real AWS; floci: "not supported by DocDB")
aws docdb failover-db-cluster --db-cluster-identifier "$C"

# snapshots (real AWS)
aws docdb create-db-cluster-snapshot --db-cluster-identifier "$C" --db-cluster-snapshot-identifier "$C-manual-1"
aws docdb delete-db-cluster-snapshot --db-cluster-snapshot-identifier "$C-manual-1"
```

## Metrics to watch

`AWS/DocDB`; dimensions `DBClusterIdentifier`, `DBClusterIdentifier,Role`
(`WRITER`/`READER`) and `DBInstanceIdentifier`:

| Metric | Notes |
|---|---|
| `CPUUtilization`, `FreeableMemory`, `SwapUsage` | per instance |
| `DatabaseConnections` / `DatabaseConnectionsMax` / `DatabaseConnectionsLimit` | every task's driver holds connections - compare with the limit of the instance class |
| `DBInstanceReplicaLag`, `DBClusterReplicaLagMaximum` | milliseconds behind the primary |
| `BufferCacheHitRatio`, `IndexBufferCacheHitRatio` | low = working set doesn't fit in memory |
| `OpcountersInsert` / `OpcountersQuery` / `DocumentsInserted` | workload (an idle cluster still shows ~50 opcounters/minute of internal activity) |
| `VolumeBytesUsed` | storage billing |
| `LowMemNumOperationsThrottled` | requests throttled for lack of memory |

```bash
aws cloudwatch get-metric-statistics --namespace AWS/DocDB --metric-name DatabaseConnections \
  --dimensions Name=DBClusterIdentifier,Value=learning-ecs-dev-docdb \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Maximum --output table
```

## Troubleshooting

| Symptom | Where to look | Typical cause / fix |
|---|---|---|
| Task stops; `ca-bundle` exit code `23` (curl: write error) | `describe-tasks` -> containers | the init container can't write the volume - it must run as root (`user="0"`), as here |
| Task stops; `ca-bundle` exits non-zero with a download error, `mongosh` never starts | `describe-tasks`, init container logs | no route to `truststore.pki.rds.amazonaws.com` (private subnets need a NAT Gateway) |
| `mongosh`: `ENOENT ... /certs/global-bundle.pem` | dependency, mounts | `mongosh` started before/without the bundle - check `dependsOn` and the mount points |
| Connection fails with a TLS/handshake error | `tls` parameter, client flags | TLS on the cluster but not in the client, or the opposite |
| `Authentication failed` | secret, auth database | wrong password in the secret, or the user isn't in `admin` |
| Writes fail mentioning retryable writes | `--retryWrites false` | retryable writes are only supported from engine 8.0.2 on (DocumentDB docs) |

More: [`../../docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md).

## floci vs real AWS

| Behavior | floci 2.1.0 | Real AWS |
|---|---|---|
| `AWS::DocDB::*` from CloudFormation | **not created** (stubbed) - use `CDK_DOCDB_ENDPOINT` with a CLI-created cluster | created |
| `docdb create-db-cluster` (CLI) | a real MongoDB container (`mongo:7.0`); endpoint = a container IP on `learning-ecs-floci-net`, port 27017 | managed cluster |
| TLS | not offered (`CDK_DOCDB_TLS=false`) | required by default |
| Task definition `volumes`, `mountPoints`, `user`, `dependsOn` registered by CloudFormation | **dropped** (`describe-task-definition` shows none) - the init-container pattern can't be exercised on floci | kept and enforced |
| `failover-db-cluster` | `not supported by DocDB` | supported |
| Adding/removing instances, backup retention | work | work |
| Metrics | not produced | produced |

## Clean up

```bash
uv run cdk destroy DocumentDbStack
uv run python scripts/floci_prune.py --apply   # floci only
# floci: the CLI-created cluster and secret
aws docdb delete-db-instance --db-instance-identifier learning-ecs-dev-docdb-1
aws docdb delete-db-cluster --db-cluster-identifier learning-ecs-dev-docdb --skip-final-snapshot
aws secretsmanager delete-secret --secret-id learning-ecs-dev-secret-documentdb --force-delete-without-recovery
```

## Notes and cautions

- **Cost**: see [Deploy to real AWS](#deploy-to-real-aws-optional).
- **The init container downloads the bundle on every task start** from
  AWS's trust store. For production, consider baking the bundle into your
  own image (Amazon ECR) so a task start never depends on that download.
- **Disabling TLS is for learning only** (and needed on floci).
- **Read preference**: point read-heavy clients at replicas with
  `readPreference=secondaryPreferred` and `replicaSet=rs0`, as DocumentDB's
  connection examples do.

## References

- [Connecting programmatically to Amazon DocumentDB](https://docs.aws.amazon.com/documentdb/latest/developerguide/connect_programmatically.html)
- [Encrypting data in transit](https://docs.aws.amazon.com/documentdb/latest/developerguide/security.encryption.ssl.html)
- [Monitoring Amazon DocumentDB with CloudWatch](https://docs.aws.amazon.com/documentdb/latest/developerguide/cloud_watch.html)
- [Retryable writes in Amazon DocumentDB](https://docs.aws.amazon.com/documentdb/latest/developerguide/retryable-writes.html)
- [Amazon ECS task definition parameters - container dependency (`dependsOn`)](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html#container_definition_dependson)
- [Use bind mounts with Amazon ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/bind-mounts.html)
- [Amazon DocumentDB pricing](https://aws.amazon.com/documentdb/pricing/)
- [AWS CDK API Reference (Python) - `aws_cdk.aws_docdb.DatabaseCluster`](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_docdb/DatabaseCluster.html)
- [Docker Hub - mongo](https://hub.docker.com/_/mongo) · [Docker Hub - curlimages/curl](https://hub.docker.com/r/curlimages/curl)
