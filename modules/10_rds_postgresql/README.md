<!-- TOC -->

- [Module 10 - RDS for PostgreSQL (a REST API with PostgREST on Fargate)](#module-10---rds-for-postgresql-a-rest-api-with-postgrest-on-fargate)
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
    - [1. Run the migration (a one-off ECS task)](#1-run-the-migration-a-one-off-ecs-task)
    - [2. Write and read through the API](#2-write-and-read-through-the-api)
    - [3. Read the table directly](#3-read-the-table-directly)
    - [List every resource with the AWS CLI](#list-every-resource-with-the-aws-cli)
  - [Manage it with the AWS CLI](#manage-it-with-the-aws-cli)
  - [Metrics to watch](#metrics-to-watch)
  - [Troubleshooting](#troubleshooting)
  - [floci vs real AWS](#floci-vs-real-aws)
  - [Clean up](#clean-up)
  - [Notes and cautions](#notes-and-cautions)
  - [References](#references)

<!-- TOC -->

# Module 10 - RDS for PostgreSQL (a REST API with PostgREST on Fargate)

## Overview

[PostgREST](https://docs.postgrest.org/) serves a PostgreSQL schema as a
REST API: every table becomes an endpoint, `GET` reads, `POST` inserts.
That makes it a perfect, real application to show an ECS service reading
*and* writing an **Amazon RDS for PostgreSQL** database without writing any
code: the official [`postgrest/postgrest`](https://hub.docker.com/r/postgrest/postgrest)
image runs on Fargate behind a public ALB, and a one-off task with the
official [`postgres`](https://hub.docker.com/_/postgres) image runs the
**schema migration** before the API can serve anything.

It builds on [module 09](../09_rds_mysql/README.md) (same database
patterns) and adds three things you meet at scale:

- **least privilege**: the API logs in as a low-privilege `authenticator`
  role with its own generated secret - only the migration task ever holds
  the master password;
- **migrations as one-off tasks**, run before (or between) deployments;
- **health checks on a separate admin port** (`/ready` on 3001, traffic on
  3000).

## What you will learn

- `rds.DatabaseInstance` for PostgreSQL (`PostgresEngineVersion.of`).
- Two secrets with two levels of privilege, and how each reaches the right
  container.
- Running a migration with `run-task`, and why PostgREST needs
  `NOTIFY pgrst, 'reload schema'` afterwards.
- ALB health checks on another port than the traffic port, and the extra
  security group rule it needs.
- Reading/writing through the API, and checking the table directly.

## Architecture

```
 internet -> ALB learning-ecs-dev-rds-pg-alb  (health check: GET /ready on port 3001)
               -> PostgREST tasks :3000 (schema "api", anonymous role web_anon)
                    PGRST_DB_URI=postgres://authenticator@<endpoint>:<port>/app
                    PGPASSWORD <- secret learning-ecs-dev-secret-rds-postgres-app
                        |  5432 (only from the API SG and the client-task SG)
                        v
               RDS for PostgreSQL learning-ecs-dev-rds-postgres  (isolated subnets)

 aws ecs run-task (family learning-ecs-dev-rds-postgres-client, image postgres:17-alpine)
   default: the migration  (schema api, table api.todos, roles web_anon + authenticator, NOTIFY pgrst)
   with SQL=...: any statement, as the master user pgadmin
```

## AWS services and CDK constructs used

| AWS service | CDK construct (Python) | Level |
|---|---|---|
| Amazon RDS | `aws_cdk.aws_rds.DatabaseInstance`, `DatabaseInstanceEngine.postgres`, `PostgresEngineVersion.of` | L2 |
| AWS Secrets Manager | `Secret` x2 (via `shared/database.py`) | L2 |
| Amazon ECS | `FargateService` (PostgREST), `FargateTaskDefinition` (migration/client), `ContainerDefinition.add_port_mappings` | L2 |
| Elastic Load Balancing | `ApplicationLoadBalancer`, `HealthCheck(port=...)` | L2 |

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CDK_RDS_POSTGRES_VERSION` | `17.9` | engine version (major derived from it) |
| `CDK_RDS_INSTANCE_TYPE`, `CDK_RDS_MULTI_AZ`, `CDK_RDS_STORAGE_GB`, `CDK_RDS_MAX_STORAGE_GB`, `CDK_RDS_BACKUP_DAYS` | as in [module 09](../09_rds_mysql/README.md#configuration) | |
| `CDK_RDS_POSTGRES_DESIRED_COUNT` | `CDK_DESIRED_COUNT` (`2`) | PostgREST tasks |
| `CDK_RDS_POSTGRES_API_IMAGE` / `CDK_RDS_POSTGRES_CLIENT_IMAGE` | `postgrest/postgrest:v16.4` / `postgres:17-alpine` | images |
| `CDK_PORT_RDS_POSTGRESQL` | `80` (`.env.example`: `8089`) | ALB listener port |

## Prerequisites

[Module 09](../09_rds_mysql/README.md). floci running and `.env` loaded.

## Tests

[`../../tests/unit/test_10_rds_postgresql.py`](../../tests/unit/test_10_rds_postgresql.py)
checks the PostgreSQL instance and the two secrets, the configurable
version, PostgREST's connection settings (as `authenticator`, password from
the app secret, ports 3000 and 3001), the `/ready` health check on 3001 and
its security group rule, the migration the client task runs with both
secrets, and the mandatory tags:

```bash
uv run pytest tests/unit/test_10_rds_postgresql.py -v
```

## Deploy with floci (local, free)

```bash
uv run cdk bootstrap
uv run cdk synth RdsPostgresqlStack
uv run cdk diff RdsPostgresqlStack
uv run cdk deploy RdsPostgresqlStack --require-approval never --method=direct
```

## Deploy to real AWS (optional)

Costs as in [module 09](../09_rds_mysql/README.md#deploy-to-real-aws-optional)
- see [Amazon RDS for PostgreSQL pricing](https://aws.amazon.com/rds/postgresql/pricing/).

```bash
unset AWS_ENDPOINT_URL CDK_PORT_RDS_POSTGRESQL
uv run cdk bootstrap --profile <your-aws-cli-profile>
uv run cdk diff RdsPostgresqlStack --profile <your-aws-cli-profile>
uv run cdk deploy RdsPostgresqlStack --profile <your-aws-cli-profile>
```

## Verify

Until the migration runs, the `authenticator` role doesn't exist:
PostgREST keeps retrying its connection, `/ready` fails, and the ALB keeps
the targets unhealthy - that's expected.

```bash
out() { aws cloudformation describe-stacks --stack-name RdsPostgresqlStack \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
CLUSTER=$(out ClusterName); FAMILY=$(out ClientTaskFamily); SG=$(out ClientSecurityGroupId)
SUBNETS=$(aws ecs describe-services --cluster "$CLUSTER" --services learning-ecs-dev-rds-postgres-api \
  --query 'services[0].networkConfiguration.awsvpcConfiguration.subnets' --output text | tr '\t' ',')
TD=$(out ClientTaskDefinitionArn)   # the exact revision this stack registered
NET="awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}"
URL=$(out Url); [ -n "${AWS_ENDPOINT_URL:-}" ] && URL=http://localhost:${CDK_PORT_RDS_POSTGRESQL:-8089}/
```

### 1. Run the migration (a one-off ECS task)

```bash
TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$TD" \
  --capacity-provider-strategy capacityProvider=FARGATE,weight=1 --network-configuration "$NET" \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode'   # 0

aws logs tail /ecs/learning-ecs/dev/rds-postgres-client --since 10m                              # real AWS
docker logs learning-ecs-floci 2>&1 | grep '\[ecs:learning-ecs-dev-rds-postgres-client' | tail -10   # floci
# CREATE SCHEMA / CREATE TABLE / DO / GRANT / GRANT / DO / ALTER ROLE / GRANT ROLE / NOTIFY
```

The migration is idempotent (`IF NOT EXISTS`, duplicate roles ignored) -
running it again is safe.

### 2. Write and read through the API

```bash
curl -s "${URL}todos"                                                       # []  (200)
curl -s -X POST "${URL}todos" -H 'Content-Type: application/json' -H 'Prefer: return=representation' \
  -d '{"task":"deploy PostgREST on ECS"}'                                   # 201, the new row
curl -s -o /dev/null -w '%{http_code}\n' -X POST "${URL}todos" -H 'Content-Type: application/json' \
  -d '[{"task":"read it back","done":false},{"task":"scale it out","done":true}]'   # 201 (bulk: same keys in every object)
curl -s "${URL}todos?done=is.false&order=id.desc&select=id,task"            # filtering, ordering, projection
```

### 3. Read the table directly

The same client task runs any SQL you put in `SQL` (as the master user):

```bash
TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$TD" \
  --capacity-provider-strategy capacityProvider=FARGATE,weight=1 --network-configuration "$NET" \
  --overrides '{"containerOverrides":[{"name":"client","environment":[{"name":"SQL","value":"SELECT id, task, done FROM api.todos ORDER BY id; SELECT current_user, version();"}]}]}' \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
docker logs learning-ecs-floci 2>&1 | grep '\[ecs:learning-ecs-dev-rds-postgres-client' | tail -10   # floci
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
`make cdk-resources STACK=RdsPostgresqlStack` runs the same commands for you.

```bash
# Match these to your deployment: CDK_PRODUCT and CDK_ENVIRONMENT in .env, and
# the region you deployed to (floci: the one in .env).
PRODUCT=learning-ecs ENV=dev REGION=us-east-1
STACK=RdsPostgresqlStack
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
aws ecs describe-clusters --clusters "${PRODUCT}-${ENV}-ecs-rds-postgres" --query "clusters[].[clusterName,status]" --output table --region "$REGION"
# AWS::SecretsManager::Secret (MasterSecretA11BF785)
aws secretsmanager describe-secret --secret-id "${PRODUCT}-${ENV}-secret-rds-postgres" --query "[Name,ARN]" --output table --region "$REGION"
# AWS::SecretsManager::Secret (AppSecretFAB5164C)
aws secretsmanager describe-secret --secret-id "${PRODUCT}-${ENV}-secret-rds-postgres-app" --query "[Name,ARN]" --output table --region "$REGION"
# AWS::RDS::DBSubnetGroup (DatabaseSubnetGroup7D60F180)
aws rds describe-db-subnet-groups --db-subnet-group-name "$(pid DatabaseSubnetGroup7D60F180)" --query "DBSubnetGroups[].DBSubnetGroupName" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (DatabaseSecurityGroup5C91FDCB)
aws ec2 describe-security-groups --group-ids "$(pid DatabaseSecurityGroup5C91FDCB)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::RDS::DBInstance (DatabaseB269D8BB)
aws rds describe-db-instances --db-instance-identifier "${PRODUCT}-${ENV}-rds-postgres" --query "DBInstances[].[DBInstanceIdentifier,Engine,DBInstanceStatus]" --output table --region "$REGION"
# AWS::IAM::Role (ApiTaskDefinitionTaskRole7EE87BD7)
aws iam get-role --role-name "$(pid ApiTaskDefinitionTaskRole7EE87BD7)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (ApiTaskDefinition51EA709E)
aws ecs describe-task-definition --task-definition "$(pid ApiTaskDefinition51EA709E)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (ApiTaskDefinitionExecutionRoleA3303016)
aws iam get-role --role-name "$(pid ApiTaskDefinitionExecutionRoleA3303016)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (ApiLogGroup1DEDFC07)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/rds-postgres-api" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ECS::Service (ApiServiceC9037CF0)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-rds-postgres" --services "${PRODUCT}-${ENV}-rds-postgres-api" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (ApiServiceSecurityGroupA2426F91)
aws ec2 describe-security-groups --group-ids "$(pid ApiServiceSecurityGroupA2426F91)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::LoadBalancer (Alb16C2F182)
aws elbv2 describe-load-balancers --load-balancer-arns "$(pid Alb16C2F182)" --query "LoadBalancers[].[LoadBalancerName,Type,State.Code]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (AlbSecurityGroup580F65A6)
aws ec2 describe-security-groups --group-ids "$(pid AlbSecurityGroup580F65A6)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::Listener (AlbHttp7966E42E)
aws elbv2 describe-listeners --listener-arns "$(pid AlbHttp7966E42E)" --query "Listeners[].[Port,Protocol]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::TargetGroup (AlbHttpApiGroup7C8FA22E)
aws elbv2 describe-target-groups --target-group-arns "$(pid AlbHttpApiGroup7C8FA22E)" --query "TargetGroups[].[TargetGroupName,Port,TargetType]" --output table --region "$REGION"
# AWS::IAM::Role (ClientTaskDefinitionTaskRole3D4CF998)
aws iam get-role --role-name "$(pid ClientTaskDefinitionTaskRole3D4CF998)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (ClientTaskDefinition0504FE38)
aws ecs describe-task-definition --task-definition "$(pid ClientTaskDefinition0504FE38)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (ClientTaskDefinitionExecutionRole6FEF8DC3)
aws iam get-role --role-name "$(pid ClientTaskDefinitionExecutionRole6FEF8DC3)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (ClientLogGroup94520E53)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/rds-postgres-client" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (ClientSecurityGroupE3B38CB0)
aws ec2 describe-security-groups --group-ids "$(pid ClientSecurityGroupE3B38CB0)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# Also created - listed in the table above:
#   15 VPC sub-resources (subnets, route tables, gateways, endpoints) - built by shared/network.py, listed one by one in modules/01_network/README.md
#   AWS::ECS::ClusterCapacityProviderAssociations Cluster3DA9CCBA - shown by its ECS cluster (describe-clusters --include ATTACHMENTS)
#   AWS::EC2::SecurityGroupIngress DatabaseSecurityGroupfromRdsPostgresqlStackApiServiceSecurityGroup53F3B5F8IndirectPortA9F5D6AE - shown by its security group
#   AWS::EC2::SecurityGroupIngress DatabaseSecurityGroupfromRdsPostgresqlStackClientSecurityGroup55A1BE56IndirectPort54D1E153 - shown by its security group
#   AWS::IAM::Policy ApiTaskDefinitionTaskRoleDefaultPolicyA678CF9F - shown by its IAM role
#   AWS::IAM::Policy ApiTaskDefinitionExecutionRoleDefaultPolicy5B03B3DE - shown by its IAM role
#   AWS::EC2::SecurityGroupIngress ApiServiceSecurityGroupfromRdsPostgresqlStackAlbSecurityGroup47296B11300036148E9A - shown by its security group
#   AWS::EC2::SecurityGroupIngress ApiServiceSecurityGroupfromRdsPostgresqlStackAlbSecurityGroup47296B1130017904503E - shown by its security group
#   AWS::EC2::SecurityGroupEgress AlbSecurityGrouptoRdsPostgresqlStackApiServiceSecurityGroup53F3B5F830008F273E4A - shown by its security group
#   AWS::EC2::SecurityGroupEgress AlbSecurityGrouptoRdsPostgresqlStackApiServiceSecurityGroup53F3B5F830014502B99E - shown by its security group
#   AWS::IAM::Policy ClientTaskDefinitionExecutionRoleDefaultPolicy7766FC0C - shown by its IAM role
```
<!-- END resource-commands -->

## Manage it with the AWS CLI

Every RDS command from [module 09](../09_rds_mysql/README.md#manage-it-with-the-aws-cli)
applies with `DB=learning-ecs-dev-rds-postgres`. PostgreSQL-specific:

```bash
# PostgreSQL parameters live in parameter groups (e.g. log slow statements)
aws rds create-db-parameter-group --db-parameter-group-name learning-ecs-dev-pg17 \
  --db-parameter-group-family postgres17 --description "learning-ecs PostgreSQL 17"
aws rds modify-db-parameter-group --db-parameter-group-name learning-ecs-dev-pg17 \
  --parameters "ParameterName=log_min_duration_statement,ParameterValue=500,ApplyMethod=immediate"
aws rds modify-db-instance --db-instance-identifier learning-ecs-dev-rds-postgres \
  --db-parameter-group-name learning-ecs-dev-pg17 --apply-immediately
aws rds describe-db-parameters --db-parameter-group-name learning-ecs-dev-pg17 \
  --query "Parameters[?ParameterName=='log_min_duration_statement'].[ParameterValue,ApplyType]"

# a new API version = a new task definition revision + update-service (module 02);
# run the migration task *before* deploying an API that needs the new schema.
```

## Metrics to watch

The `AWS/RDS` metrics of [module 09](../09_rds_mysql/README.md#metrics-to-watch)
(dimension `DBInstanceIdentifier=learning-ecs-dev-rds-postgres`), plus the
PostgreSQL-only ones: `MaximumUsedTransactionIDs` (transaction ID
wraparound), `TransactionLogsDiskUsage`, `TransactionLogsGeneration`,
`ReplicationSlotDiskUsage`, `OldestReplicationSlotLag`.

On the API side: the ALB's `TargetResponseTime` and
`HTTPCode_Target_5XX_Count` ([module 04](../04_alb/README.md#metrics-to-watch)) -
PostgREST answers database errors with 4xx/5xx JSON bodies (`code`,
`message`), which also appear in its logs.

## Troubleshooting

| Symptom | Where to look | Typical cause / fix |
|---|---|---|
| Targets unhealthy, PostgREST logs connection errors for `authenticator` | API logs | the migration hasn't run yet (the role doesn't exist) - run it |
| `404` for `/todos` after the migration, with a PostgREST error saying the table isn't in its schema cache | API logs | PostgREST's schema cache is stale - the migration's `NOTIFY pgrst, 'reload schema'` reloads it; send it again with the client task, or restart the tasks |
| POST rejected with PostgreSQL error code `42501` (insufficient privilege) | grants | `web_anon` lacks `INSERT` on the table |
| `{"code":"PGRST102"...}` on a bulk POST | request body | every object in the array must have the same keys |
| Migration task exits with code 3 | client logs | an SQL error (`ON_ERROR_STOP=1` makes psql stop at the first one) |
| Health check fails although the API answers on 3000 | target group health check port | `/ready` is on the admin port (3001) - its security group rule must exist (it does here) |

More: [`../../docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md).

## floci vs real AWS

Everything from [module 09's table](../09_rds_mysql/README.md#floci-vs-real-aws)
applies. In addition:

| Behavior | floci 2.1.0 | Real AWS |
|---|---|---|
| Engine version | runs the version asked for (`PostgreSQL 17.9` in `SELECT version()`) | same |
| Custom DB parameter groups | created and attachable, but a parameter set with `modify-db-parameter-group` isn't listed back by `describe-db-parameters`, and the instance keeps reporting `default.postgres17` | applied (static parameters after a reboot) |
| ALB health check on another port (`/ready` on 3001) | works | works |

## Clean up

```bash
uv run cdk destroy RdsPostgresqlStack
uv run python scripts/floci_prune.py --apply   # floci only
```

## Notes and cautions

- **Anonymous writes are for learning only.** `web_anon` may `INSERT`
  here so you can test without tokens. Real PostgREST deployments
  authenticate with JWTs and map them to roles - see PostgREST's
  [tutorial 1](https://docs.postgrest.org/en/stable/tutorials/tut1.html).
- **Migrations and deployments**: run backwards-compatible migrations
  before deploying the code that needs them, so old and new tasks (which
  run side by side during a rolling deployment) both work.
- **Pin image tags** - PostgREST publishes frequent releases; re-check
  [its releases](https://github.com/PostgREST/postgrest/releases) before
  changing `v16.4`.

## References

- [Amazon RDS for PostgreSQL](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_PostgreSQL.html)
- [Working with DB parameter groups](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithParamGroups.html)
- [Amazon CloudWatch metrics for Amazon RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-metrics.html)
- [Health checks for Application Load Balancer target groups](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-health-checks.html)
- [PostgREST - configuration](https://docs.postgrest.org/en/stable/references/configuration.html) · [tutorial 0](https://docs.postgrest.org/en/stable/tutorials/tut0.html)
- [AWS CDK API Reference (Python) - `aws_cdk.aws_rds.DatabaseInstance`](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_rds/DatabaseInstance.html)
- [Docker Hub - postgrest/postgrest](https://hub.docker.com/r/postgrest/postgrest) · [Docker Hub - postgres](https://hub.docker.com/_/postgres)
