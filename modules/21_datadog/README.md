<!-- TOC -->

- [Module 21 - Datadog (Agent sidecar, FireLens logs, unified service tagging, Autodiscovery)](#module-21---datadog-agent-sidecar-firelens-logs-unified-service-tagging-autodiscovery)
  - [Overview](#overview)
  - [What you will learn](#what-you-will-learn)
  - [Architecture](#architecture)
  - [AWS services and CDK constructs used](#aws-services-and-cdk-constructs-used)
  - [Configuration](#configuration)
  - [How the pieces fit](#how-the-pieces-fit)
    - [The Datadog Agent sidecar](#the-datadog-agent-sidecar)
    - [Logs through FireLens](#logs-through-firelens)
    - [Unified service tagging](#unified-service-tagging)
    - [Autodiscovery: the NGINX check](#autodiscovery-the-nginx-check)
    - [Custom metrics and traces](#custom-metrics-and-traces)
  - [Prerequisites](#prerequisites)
  - [Tests](#tests)
  - [Deploy with floci (local, free)](#deploy-with-floci-local-free)
  - [Deploy to real AWS (optional)](#deploy-to-real-aws-optional)
  - [Verify](#verify)
    - [List every resource with the AWS CLI](#list-every-resource-with-the-aws-cli)
  - [Manage it with the AWS CLI](#manage-it-with-the-aws-cli)
    - [The API key](#the-api-key)
    - [Generate traffic and errors](#generate-traffic-and-errors)
    - [Ask the Agent (ECS Exec)](#ask-the-agent-ecs-exec)
  - [Metrics to watch](#metrics-to-watch)
  - [Troubleshooting](#troubleshooting)
  - [floci vs real AWS](#floci-vs-real-aws)
  - [Clean up](#clean-up)
  - [Notes and cautions](#notes-and-cautions)
  - [References](#references)

<!-- TOC -->

# Module 21 - Datadog (Agent sidecar, FireLens logs, unified service tagging, Autodiscovery)

## Overview

On Fargate there is no host to install an agent on, so **Datadog's Agent
runs as a sidecar container in every task**. This module deploys an
[`nginx`](https://hub.docker.com/_/nginx) service (the same JSON access
logs as [module 19](../19_cloudwatch/README.md)) instrumented the way
Datadog's ECS Fargate documentation describes:

- the **Datadog Agent** ([`datadog/agent`](https://hub.docker.com/r/datadog/agent))
  with `ECS_FARGATE=true` collects the task's container metrics, runs the
  **NGINX check** found through **Autodiscovery** labels, and accepts
  **DogStatsD** metrics and **APM traces** from the app;
- a **FireLens** log router ([`amazon/aws-for-fluent-bit`](https://hub.docker.com/r/amazon/aws-for-fluent-bit))
  ships the app's logs straight to Datadog's logs intake;
- **unified service tagging** (`env`, `service`, `version`) ties metrics,
  logs and traces of the service together;
- the **API key** lives in AWS Secrets Manager and reaches the Agent and
  the log router as container secrets - never in the task definition.

You need a Datadog account (a trial works) to see the data; without one,
everything deploys and runs, but nothing is accepted by Datadog.

## What you will learn

- The sidecar pattern for Fargate monitoring agents.
- FireLens: routing container logs with Fluent Bit, secrets in log options.
- Unified service tagging and Autodiscovery labels in a task definition.
- Operating the Agent: `agent status`, `agent check`, `agent health` via ECS Exec.

## Architecture

```
 load task (curl) --> ALB :8098 --> service learning-ecs-dev-dd-web (2 tasks), each task:
   +-----------------------------------------------------------------------------------------+
   | app (nginx :80, stub_status :81)       datadog-agent (ECS_FARGATE)     log_router         |
   |  env DD_ENV/DD_SERVICE/DD_VERSION  <--  NGINX check (Autodiscovery)    (fluent bit)       |
   |  labels com.datadoghq.tags.*           task metrics (metadata endpoint)                    |
   |  logDriver awsfirelens ------------------------------------------------> Datadog logs     |
   |  DogStatsD -> localhost:8125/udp, traces -> localhost:8126 --> agent --> Datadog metrics/APM |
   +-----------------------------------------------------------------------------------------+
 Secrets Manager learning-ecs-dev-secret-datadog-api-key --> DD_API_KEY (agent), apikey (FireLens)
 CloudWatch Logs: only the agent's and the log router's own output (for their troubleshooting)
```

## AWS services and CDK constructs used

| AWS service | CDK construct (Python) | Level |
|---|---|---|
| Amazon ECS | `FargateService` (via `shared/ecs.py`), `TaskDefinition.add_container` | L2 |
| Amazon ECS (FireLens) | `TaskDefinition.add_firelens_log_router`, `FirelensConfig`, `LogDrivers.firelens` | L2 |
| AWS Secrets Manager | `Secret`, `Secret.from_secret_name_v2`, `ecs.Secret.from_secrets_manager` | L2 |
| Elastic Load Balancing | `ApplicationLoadBalancer`, `FargateService.load_balancer_target` | L2 |

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CDK_DATADOG_SITE` | `datadoghq.com` | your Datadog site (e.g. `datadoghq.eu`, `us3.datadoghq.com`); also builds the logs host `http-intake.logs.<site>` |
| `CDK_DATADOG_API_KEY_SECRET_NAME` | unset | an existing Secrets Manager secret holding the API key (plain string); unset: the stack creates `<product>-<env>-secret-datadog-api-key` with a random placeholder |
| `CDK_DATADOG_SERVICE` | `<product>-<env>-web` | unified service tagging `service` |
| `CDK_DATADOG_VERSION` | `v1` | unified service tagging `version` (use your image tag or commit) |
| `CDK_DATADOG_DESIRED_COUNT` | `2` (or `CDK_DESIRED_COUNT`) | tasks |
| `CDK_DATADOG_AGENT_IMAGE` / `CDK_DATADOG_LOG_ROUTER_IMAGE` / `CDK_DATADOG_IMAGE` | `datadog/agent:7` / `amazon/aws-for-fluent-bit:stable` / `nginx:1.30-alpine` | images |
| `CDK_DATADOG_LOAD_WORKERS` / `..._LOAD_SECONDS` / `..._LOAD_ERROR_EVERY` | `5` / `300` / `20` | load generator |
| `CDK_PORT_DATADOG` | `8098` (`.env.example`) | ALB listener port (80 if unset) |

`env` is `CDK_ENVIRONMENT` - the same value as the mandatory `environment` tag.

## How the pieces fit

### The Datadog Agent sidecar

From [Datadog - Amazon ECS on AWS Fargate](https://docs.datadoghq.com/integrations/ecs_fargate/):

- `ECS_FARGATE=true` tells the Agent it runs in a Fargate task: it reads
  the task's containers and their CPU, memory, disk and network usage from
  the **ECS task metadata endpoint** (`ecs.fargate.*` metrics).
- `DD_API_KEY` (here a Secrets Manager secret) and `DD_SITE` say where to
  send the data.
- The health check is `agent health`.
- `DD_APM_ENABLED=true` turns on the trace intake (8126/tcp); DogStatsD
  listens on 8125/udp. Containers of the same Fargate task share
  `localhost`, so the app reaches both there - don't set `DD_AGENT_HOST`
  on Fargate.
- For the Datadog **AWS integration** to add ECS metadata, its IAM role
  needs `ecs:ListClusters`, `ecs:ListContainerInstances` and
  `ecs:DescribeContainerInstances` (configured on the Datadog side, not in
  this stack).

### Logs through FireLens

The app container's log driver is `awsfirelens`: ECS hands its stdout and
stderr to the `log_router` container (Fluent Bit, `enable-ecs-log-metadata`
adds the cluster, task and container to every record), which sends them
with the `datadog` output:

| Option | Value here |
|---|---|
| `Name` | `datadog` |
| `Host` | `http-intake.logs.<CDK_DATADOG_SITE>` |
| `TLS` / `provider` | `on` / `ecs` |
| `dd_service`, `dd_source`, `dd_tags` | the service, `nginx`, `env:...,version:...,team:...` |
| `dd_message_key` | `log` |
| `apikey` | **secretOptions** - from Secrets Manager, never plain text |

The JSON access lines become structured attributes in Datadog. The Agent
and the router themselves log to CloudWatch (`/ecs/learning-ecs/dev/dd-web-agent`,
`/ecs/learning-ecs/dev/dd-web-log-router`) so you can debug them even
when the Datadog path is broken.

### Unified service tagging

The three reserved tags go **on the application container, not the
Agent** (Datadog's ECS instructions):

```text
environment:  DD_ENV=dev  DD_SERVICE=learning-ecs-dev-web  DD_VERSION=v1
dockerLabels: com.datadoghq.tags.env=dev  com.datadoghq.tags.service=learning-ecs-dev-web  com.datadoghq.tags.version=v1
```

Change `CDK_DATADOG_VERSION` at each release and Datadog can compare
versions (errors, latency) across a deployment.

### Autodiscovery: the NGINX check

Three docker labels on the app container tell the Agent to run its NGINX
check against `http://%%host%%:81/nginx_status/` (`%%host%%` is resolved by
the Agent to the container's address):

```text
com.datadoghq.ad.check_names = ["nginx"]
com.datadoghq.ad.init_configs = [{}]
com.datadoghq.ad.instances   = [{"nginx_status_url": "http://%%host%%:81/nginx_status/"}]
```

nginx serves `stub_status` on port 81; that port is neither mapped to the
load balancer nor opened in the task's security group.

### Custom metrics and traces

The Agent is ready for application instrumentation - not used by nginx
here:

- **DogStatsD**: send UDP datagrams to `localhost:8125` from the app, in the
  form `metric.name:value|type|#tag:value`, e.g.
  `printf 'orders.created:1|c|#env:dev' | nc -u -w1 127.0.0.1 8125`.
- **APM**: Datadog tracing libraries send traces to `localhost:8126`; with
  `DD_ENV`/`DD_SERVICE`/`DD_VERSION` already set, the traces carry the same
  tags as the metrics and logs.

## Prerequisites

[Module 04](../04_alb/README.md). A Datadog account and an **API key**
(Organization Settings > API Keys) to see the data. floci running and
`.env` loaded.

## Tests

[`../../tests/unit/test_21_datadog.py`](../../tests/unit/test_21_datadog.py)
checks the three essential containers and images, the Agent's Fargate
mode, API key secret, health check and ports, the FireLens log
configuration (Datadog output, host, TLS, `apikey` secret option) and
router, unified service tagging and Autodiscovery labels, the configurable
site and existing key, the placeholder secret, the load balancer target
container, and the mandatory tags:

```bash
uv run pytest tests/unit/test_21_datadog.py -v
```

## Deploy with floci (local, free)

```bash
uv run cdk bootstrap
uv run cdk synth DatadogStack
uv run cdk deploy DatadogStack --require-approval never --method=direct
curl -s localhost:8098/          # hello from 752a611f5358
docker logs learning-ecs-floci 2>&1 | grep -E 'ecs:learning-ecs-dev-dd-web:(app|datadog-agent|log_router)' | tail -5
```

All three containers start on floci, but floci drops the log and FireLens
configuration and gives the Agent no task metadata endpoint - see
[floci vs real AWS](#floci-vs-real-aws). Real data in Datadog needs real
AWS and a real API key.

## Deploy to real AWS (optional)

Cost: the tasks (three containers each - size the task for the Agent too),
the ALB and the NAT Gateway on AWS; Datadog bills per its own pricing
(infrastructure, logs ingested/indexed, APM) - see
[Datadog pricing](https://www.datadoghq.com/pricing/).

```bash
unset AWS_ENDPOINT_URL
# 1. your key in Secrets Manager (or reuse one: CDK_DATADOG_API_KEY_SECRET_NAME=<name>)
CDK_PORT_DATADOG=80 CDK_DATADOG_SITE=datadoghq.com uv run cdk deploy DatadogStack --profile <your-aws-cli-profile>
# 2. replace the placeholder with your API key, then restart the tasks - see "The API key" below
```

## Verify

```bash
out() { aws cloudformation describe-stacks --stack-name DatadogStack \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
C=$(out ClusterName); SVC=$(out ServiceName)

# the three containers of each task, and their health
T=$(aws ecs list-tasks --cluster "$C" --service-name "$SVC" --query 'taskArns[0]' --output text)
aws ecs describe-tasks --cluster "$C" --tasks "$T" \
  --query 'tasks[0].containers[].[name,lastStatus,healthStatus]' --output table

# the Agent's and the router's own logs (real AWS)
aws logs tail /ecs/learning-ecs/dev/dd-web-agent --since 10m
aws logs tail /ecs/learning-ecs/dev/dd-web-log-router --since 10m
```

In Datadog (real AWS, real key), after a few minutes:

- **Metrics**: `ecs.fargate.cpu.percent` and `ecs.fargate.mem.usage` by
  `task_arn`/`container_name`; `nginx.net.request_per_s` from the NGINX check.
- **Logs**: `service:learning-ecs-dev-web` - the nginx JSON lines, with
  `status` and `path` as attributes.
- **Service catalog / APM**: the `learning-ecs-dev-web` service with
  `env:dev`, `version:v1` (traces once an app is instrumented).

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
`make cdk-resources STACK=DatadogStack` runs the same commands for you.

```bash
# Match these to your deployment: CDK_PRODUCT and CDK_ENVIRONMENT in .env, and
# the region you deployed to (floci: the one in .env).
PRODUCT=learning-ecs ENV=dev REGION=us-east-1
STACK=DatadogStack
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
aws ecs describe-clusters --clusters "${PRODUCT}-${ENV}-ecs-dd" --query "clusters[].[clusterName,status]" --output table --region "$REGION"
# AWS::SecretsManager::Secret (ApiKeyF9DDEE66)
aws secretsmanager describe-secret --secret-id "${PRODUCT}-${ENV}-secret-datadog-api-key" --query "[Name,ARN]" --output table --region "$REGION"
# AWS::IAM::Role (WebTaskDefinitionTaskRole2EE1C0E7)
aws iam get-role --role-name "$(pid WebTaskDefinitionTaskRole2EE1C0E7)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (WebTaskDefinition8DF7C630)
aws ecs describe-task-definition --task-definition "$(pid WebTaskDefinition8DF7C630)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (WebTaskDefinitionExecutionRole225B46C9)
aws iam get-role --role-name "$(pid WebTaskDefinitionExecutionRole225B46C9)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::Service (WebService7F8A1763)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-dd" --services "${PRODUCT}-${ENV}-dd-web" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (WebServiceSecurityGroupE736A6BB)
aws ec2 describe-security-groups --group-ids "$(pid WebServiceSecurityGroupE736A6BB)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::Logs::LogGroup (LogRouterLogGroup5BD92DEB)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/dd-web-log-router" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::Logs::LogGroup (AgentLogGroupFDA72973)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/dd-web-agent" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::LoadBalancer (Alb16C2F182)
aws elbv2 describe-load-balancers --load-balancer-arns "$(pid Alb16C2F182)" --query "LoadBalancers[].[LoadBalancerName,Type,State.Code]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (AlbSecurityGroup580F65A6)
aws ec2 describe-security-groups --group-ids "$(pid AlbSecurityGroup580F65A6)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::Listener (AlbHttp7966E42E)
aws elbv2 describe-listeners --listener-arns "$(pid AlbHttp7966E42E)" --query "Listeners[].[Port,Protocol]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::TargetGroup (AlbHttpWebGroupFB8A8200)
aws elbv2 describe-target-groups --target-group-arns "$(pid AlbHttpWebGroupFB8A8200)" --query "TargetGroups[].[TargetGroupName,Port,TargetType]" --output table --region "$REGION"
# AWS::IAM::Role (LoadTaskDefinitionTaskRole0B565716)
aws iam get-role --role-name "$(pid LoadTaskDefinitionTaskRole0B565716)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (LoadTaskDefinition4E7F9FC0)
aws ecs describe-task-definition --task-definition "$(pid LoadTaskDefinition4E7F9FC0)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (LoadTaskDefinitionExecutionRole23E146C1)
aws iam get-role --role-name "$(pid LoadTaskDefinitionExecutionRole23E146C1)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (LoadLogGroup49CDBF63)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/dd-load" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (LoadSecurityGroup7D3309A0)
aws ec2 describe-security-groups --group-ids "$(pid LoadSecurityGroup7D3309A0)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# Also created - listed in the table above:
#   11 VPC sub-resources (subnets, route tables, gateways, endpoints) - built by shared/network.py, listed one by one in modules/01_network/README.md
#   AWS::ECS::ClusterCapacityProviderAssociations Cluster3DA9CCBA - shown by its ECS cluster (describe-clusters --include ATTACHMENTS)
#   AWS::IAM::Policy WebTaskDefinitionTaskRoleDefaultPolicyD1A8300E - shown by its IAM role
#   AWS::IAM::Policy WebTaskDefinitionExecutionRoleDefaultPolicy5D8A2D6F - shown by its IAM role
#   AWS::EC2::SecurityGroupIngress WebServiceSecurityGroupfromDatadogStackAlbSecurityGroup2DB2FA0180B3E1A9BD - shown by its security group
#   AWS::EC2::SecurityGroupEgress AlbSecurityGrouptoDatadogStackWebServiceSecurityGroupCC7C26AA80A70FDD0F - shown by its security group
#   AWS::IAM::Policy LoadTaskDefinitionExecutionRoleDefaultPolicy7F5AA8E1 - shown by its IAM role
```
<!-- END resource-commands -->

## Manage it with the AWS CLI

Run against floci while writing this module (except where noted).

### The API key

```bash
# put your key in the secret the stack created (a plain string, not JSON)
aws secretsmanager put-secret-value --secret-id "$(out ApiKeySecretName)" --secret-string "<your-datadog-api-key>"
# containers read secrets when they start: restart the tasks
aws ecs update-service --cluster "$C" --service "$SVC" --force-new-deployment
aws ecs wait services-stable --cluster "$C" --services "$SVC"

# rotate: the same two steps with the new key (then revoke the old one in Datadog)
```

### Generate traffic and errors

```bash
NET="awsvpcConfiguration={subnets=[$(out TaskSubnetIds)],securityGroups=[$(out LoadSecurityGroupId)],assignPublicIp=DISABLED}"
aws ecs run-task --cluster "$C" --task-definition "$(out LoadTaskDefinitionArn)" --launch-type FARGATE \
  --network-configuration "$NET" \
  --overrides '{"containerOverrides":[{"name":"app","environment":[{"name":"ERROR_EVERY","value":"5"}]}]}'
# Datadog (real AWS): logs with status:500 for service:learning-ecs-dev-web
```

### Ask the Agent (ECS Exec)

Real AWS (floci doesn't implement ECS Exec). Needs the Session Manager
plugin for the AWS CLI; the services here have ECS Exec enabled
(`CDK_ENABLE_EXECUTE_COMMAND`, default `true`).

```bash
T=$(aws ecs list-tasks --cluster "$C" --service-name "$SVC" --query 'taskArns[0]' --output text)
aws ecs execute-command --cluster "$C" --task "$T" --container datadog-agent --interactive --command "agent status"
aws ecs execute-command --cluster "$C" --task "$T" --container datadog-agent --interactive --command "agent check nginx"
aws ecs execute-command --cluster "$C" --task "$T" --container datadog-agent --interactive --command "agent health"
aws ecs execute-command --cluster "$C" --task "$T" --container datadog-agent --interactive --command "agent configcheck"
aws ecs execute-command --cluster "$C" --task "$T" --container datadog-agent --interactive --command "agent diagnose"
# for Datadog support: agent flare
```

On floci, read the Agent's output instead:
`docker logs learning-ecs-floci 2>&1 | grep 'ecs:learning-ecs-dev-dd-web:datadog-agent' | grep -E '\| (ERROR|WARN) \|'`.

## Metrics to watch

| Source | Metric | Watch for |
|---|---|---|
| Agent (Fargate) | `ecs.fargate.cpu.percent`, `ecs.fargate.cpu.usage`, `ecs.fargate.cpu.limit` | CPU per container |
| Agent (Fargate) | `ecs.fargate.mem.usage`, `ecs.fargate.mem.limit`, `ecs.fargate.mem.max_usage` | memory against the limit (OOM risk) |
| Agent (Fargate) | `ecs.fargate.io.bytes.read`/`write`, `ecs.fargate.net.bytes_rcvd`/`sent` | disk and network |
| Agent (Fargate) | service check `fargate_check` | `CRITICAL` when the Agent can't reach the Fargate metadata |
| NGINX check | `nginx.net.request_per_s`, `nginx.connections.active`, `nginx.requests.total` | traffic as nginx sees it |
| Logs | `status:>=500` on `service:learning-ecs-dev-web` | errors (turn into a log-based metric or monitor in Datadog) |
| AWS (CloudWatch) | `AWS/ApplicationELB` `HTTPCode_Target_5XX_Count`, `TargetResponseTime` | the outside view - also available in Datadog through its AWS integration |

## Troubleshooting

| Symptom | Where to look | Typical cause / fix |
|---|---|---|
| No data at all in Datadog | Agent log group, `agent status` | wrong/placeholder API key (forbidden in the forwarder status), wrong `DD_SITE`, no egress to the internet (NAT, security group) |
| Metrics but no logs | router log group | wrong `Host` for your site, `apikey` secret not readable by the execution role, router container crashed |
| Logs arrive as plain text | log attributes in Datadog | not JSON, or `dd_message_key` doesn't match the field Fluent Bit uses (`log`) |
| No `nginx.*` metrics | `agent configcheck`, `agent check nginx` | Autodiscovery labels on the wrong container, stub_status port/path wrong |
| `fargate_check` CRITICAL / no `ecs.fargate.*` | Agent log (`failed to get Fargate host`) | `ECS_FARGATE` missing, or the task metadata endpoint unreachable |
| Task stops when the Agent restarts | `describe-tasks` (`essential`) | the Agent is essential here; make it non-essential if monitoring must never take the app down |
| Logs/metrics missing `env`/`service`/`version` | the app container | UST env vars and labels must be on the **app** container |

More, including the CloudWatch and Prometheus/Grafana sides: [`../../docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md).

## floci vs real AWS

| Behavior | floci 2.1.0 | Real AWS |
|---|---|---|
| The three containers (app, Agent, log router) | start and run | same |
| Container secrets (`DD_API_KEY`) | resolved | resolved |
| `logConfiguration` / `firelensConfiguration` from CloudFormation | **dropped** (`describe-task-definition` shows none) - app logs only in `docker logs learning-ecs-floci` | FireLens routes the logs |
| ECS task metadata endpoint | **not provided** - the Agent logs `failed to get Fargate host` and its forwarder never starts | provided |
| Sending to Datadog | nothing is sent (no metadata, placeholder key) | with a real key |
| ECS Exec (`agent status`) | `UnsupportedOperation` | works |
| Container health checks (`agent health`) | not run by floci (the image's own Docker health check shows `unhealthy`) | run |

## Clean up

```bash
uv run cdk destroy DatadogStack
uv run python scripts/floci_prune.py --apply   # floci only
```

The placeholder secret is deleted with the stack; a secret you passed in
`CDK_DATADOG_API_KEY_SECRET_NAME` is not.

## Notes and cautions

- **Cost**: Datadog bills per host/task, per GB of logs and per indexed
  span - check the volume of `nginx` access logs before indexing all of them.
- **Secrets**: the API key never appears in the task definition; give the
  execution role read access to that one secret only (the CDK does this).
- **Sizing**: the Agent and the router use CPU and memory in every task;
  this module gives the task 512 CPU / 1024 MiB for three containers.
- **The other routes**: Datadog also documents the `awslogs` driver +
  the Datadog Lambda forwarder for logs, and CloudWatch metrics through
  its AWS integration - less detail, no sidecar.

## References

- [Datadog - Amazon ECS on AWS Fargate](https://docs.datadoghq.com/integrations/ecs_fargate/) · [Unified service tagging](https://docs.datadoghq.com/getting_started/tagging/unified_service_tagging/)
- [Datadog - NGINX integration](https://docs.datadoghq.com/integrations/nginx/) · [Agent commands](https://docs.datadoghq.com/agent/configuration/agent-commands/)
- [Amazon ECS - FireLens (send logs to an AWS service or AWS Partner)](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_firelens.html)
- [Amazon ECS Exec](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-exec.html)
- Docker Hub: [datadog/agent](https://hub.docker.com/r/datadog/agent) · [amazon/aws-for-fluent-bit](https://hub.docker.com/r/amazon/aws-for-fluent-bit) · [nginx](https://hub.docker.com/_/nginx) · [curlimages/curl](https://hub.docker.com/r/curlimages/curl)
