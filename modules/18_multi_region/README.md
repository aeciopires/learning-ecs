<!-- TOC -->

- [Module 18 - Multi-region (two regions, CloudFront origin failover)](#module-18---multi-region-two-regions-cloudfront-origin-failover)
  - [Overview](#overview)
  - [What you will learn](#what-you-will-learn)
  - [Architecture](#architecture)
  - [AWS services and CDK constructs used](#aws-services-and-cdk-constructs-used)
  - [Configuration](#configuration)
  - [How CloudFront origin failover works](#how-cloudfront-origin-failover-works)
  - [What about the data?](#what-about-the-data)
  - [Prerequisites](#prerequisites)
  - [Tests](#tests)
  - [Deploy with floci (local, free)](#deploy-with-floci-local-free)
  - [Deploy to real AWS (optional)](#deploy-to-real-aws-optional)
  - [Verify](#verify)
    - [List every resource with the AWS CLI](#list-every-resource-with-the-aws-cli)
  - [Manage it with the AWS CLI](#manage-it-with-the-aws-cli)
    - [A regional failover drill](#a-regional-failover-drill)
    - [Operating two regions](#operating-two-regions)
    - [Tune the failover](#tune-the-failover)
  - [Metrics to watch](#metrics-to-watch)
  - [Troubleshooting](#troubleshooting)
  - [floci vs real AWS](#floci-vs-real-aws)
  - [Clean up](#clean-up)
  - [Notes and cautions](#notes-and-cautions)
  - [References](#references)

<!-- TOC -->

# Module 18 - Multi-region (two regions, CloudFront origin failover)

## Overview

Multi-AZ (every other module here: subnets and tasks in two or more
Availability Zones) protects you from losing a data center. **Multi-region**
protects you from losing a whole region - or lets you serve users closer
to where they are. It costs a full second copy of the infrastructure and,
above all, a plan for the data.

This module deploys the **same application in two regions** - two
independent stacks from the same code, each with its own VPC, cluster, ALB
and [`traefik/whoami`](https://hub.docker.com/r/traefik/whoami) service -
and puts a **CloudFront distribution with an origin group** in front:
every request goes to the primary region first, and to the secondary
region when the primary fails (5XX, connection failure or timeout).

| Stack | Region | Contents |
|---|---|---|
| `MultiRegionPrimaryStack` | `CDK_MULTI_REGION_PRIMARY` (default: your region, else `us-east-1`) | VPC, cluster, ALB, service |
| `MultiRegionSecondaryStack` | `CDK_MULTI_REGION_SECONDARY` (default `us-west-2`) | the same |
| `MultiRegionGlobalStack` | the primary region (CloudFront itself is global) | distribution + origin group; created once both ALB DNS names are set |

## What you will learn

- One CDK app, several **environments** (account/region pairs): the same
  stack class deployed twice.
- CloudFront **origin groups**: failover criteria, timeouts, which
  requests can fail over.
- Running a regional failover drill and operating two copies of a service.
- The data question: what each AWS data service offers across regions.

## Architecture

```
                         CloudFront (global) - origin group
                         primary first; on 500/502/503/504, connection failure or timeout -> secondary
                         (GET, HEAD, OPTIONS only)
                            |                                   |
                            v                                   v
        us-east-1 (primary)                         us-west-2 (secondary)
        ALB learning-ecs-dev-alb-multi-...          ALB learning-ecs-dev-alb-multi-...
          -> service learning-ecs-dev-multi-region-web   -> service learning-ecs-dev-multi-region-web
             (2 tasks, 2 AZs)  Name: primary us-east-1      (2 tasks, 2 AZs)  Name: secondary us-west-2
        VPC 10.x, cluster learning-ecs-dev-ecs-multi-region (same names in each region)
```

## AWS services and CDK constructs used

| AWS service | CDK construct (Python) | Level |
|---|---|---|
| AWS CDK | `cdk.Environment` per stack; `build_stacks()` in [`stack.py`](stack.py), called by [`app.py`](../../app.py) | - |
| Amazon CloudFront | `aws_cdk.aws_cloudfront.Distribution`, `aws_cdk.aws_cloudfront_origins.OriginGroup`, `HttpOrigin` | L2 |
| Elastic Load Balancing | `ApplicationLoadBalancer` (one per region) | L2 |
| Amazon ECS | `FargateService` (via `shared/ecs.py`, one per region) | L2 |

`app.py` normally instantiates one stack per module. A module that defines
`build_stacks(app, *, config, env)` builds its own stacks instead - this
is that module.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CDK_MULTI_REGION_PRIMARY` | the app's region, else `us-east-1` | primary region |
| `CDK_MULTI_REGION_SECONDARY` | `us-west-2` | secondary region (must differ) |
| `CDK_MULTI_REGION_PRIMARY_ORIGIN` / `..._SECONDARY_ORIGIN` | unset | the two ALB DNS names (outputs `AlbDnsName`); with both set, `MultiRegionGlobalStack` exists. `.env.example`: `localhost` (floci) |
| `CDK_MULTI_REGION_PRIMARY_ORIGIN_PORT` / `..._SECONDARY_ORIGIN_PORT` | `80` | origin ports (`.env.example`: `8094` / `8099`) |
| `CDK_MULTI_REGION_CONNECTION_ATTEMPTS` | `1` | CloudFront connection attempts per origin (1-3) |
| `CDK_MULTI_REGION_CONNECTION_TIMEOUT` | `3` | seconds per connection attempt (1-10) |
| `CDK_MULTI_REGION_READ_TIMEOUT` | `10` | seconds to wait for a response (1-120) |
| `CDK_MULTI_REGION_DESIRED_COUNT` | `2` (or `CDK_DESIRED_COUNT`) | tasks per region |
| `CDK_MULTI_REGION_IMAGE` | `traefik/whoami:v1.12.0` | image |
| `CDK_PORT_MULTI_REGION` / `CDK_PORT_MULTI_REGION_SECONDARY` | `8094` / `8099` (`.env.example`) | ALB listener ports (80 if unset) |

## How CloudFront origin failover works

From [Optimize high availability with CloudFront origin failover](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/high_availability_origin_failover.html):

- **Every request goes to the primary first**, even right after another
  request failed over. Failover is per request - there's no "switch" to
  flip back.
- CloudFront fails over when the primary returns a **status code you
  configured** (any of 400, 403, 404, 416, 429, 500, 502, 503, 504), when
  it **can't connect** (with 503 configured) or when the primary **times
  out** (with 504 configured). This module uses 500, 502, 503 and 504.
- Only **`GET`, `HEAD` and `OPTIONS`** fail over (`OPTIONS` only if it is
  a cached method) - a `POST` to a dead primary fails. Multi-region
  *writes* need more than an origin group.
- By default CloudFront tries the primary **3 times x 10 seconds** before
  failing over; this module sets **1 attempt x 3 seconds** so a dead
  region costs a few seconds per request, not 30.

Alternatives with other trade-offs: **Route 53 failover/latency records**
with health checks (DNS-level, needs a hosted zone and a domain - see
[Configuring DNS failover](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/dns-failover-configuring.html)),
and **AWS Global Accelerator** (anycast IPs in front of regional
endpoints).

## What about the data?

The service here is stateless - which is exactly why the drill below
works so easily. Real services read and write data, and the copy in the
other region has to be **there** and **current enough**. Each data
service has its own cross-region feature (the modules that use them are
single-region on purpose):

| Data in... | Cross-region feature | Read more |
|---|---|---|
| Aurora ([module 11](../11_aurora/README.md)) | Aurora Global Database: a primary cluster and read-only secondary clusters in other regions; switchover/failover promotes a secondary | [Using Amazon Aurora Global Database](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-global-database.html) |
| RDS MySQL/PostgreSQL ([09](../09_rds_mysql/README.md), [10](../10_rds_postgresql/README.md)) | cross-region read replicas, promoted on failover | [Working with DB instance read replicas](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_ReadRepl.html) |
| ElastiCache ([12](../12_elasticache/README.md)) | Global Datastore | [Replication across AWS Regions using global datastores](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Redis-Global-Datastore.html) |
| DocumentDB ([13](../13_documentdb/README.md)) | global clusters | [Using Amazon DocumentDB global clusters](https://docs.aws.amazon.com/documentdb/latest/developerguide/global-clusters.html) |
| S3 ([15](../15_s3/README.md)) | replication (Cross-Region Replication) | [Replicating objects within and across Regions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html) |
| Secrets Manager | replica secrets in other regions | [Replicate secrets across Regions](https://docs.aws.amazon.com/secretsmanager/latest/userguide/create-manage-multi-region-secrets.html) |

Two numbers frame every one of these decisions: **RPO** (how much data you
may lose - the replication lag) and **RTO** (how long recovery may take -
including promoting the secondary database and pointing the application
at it).

## Prerequisites

[Module 04](../04_alb/README.md) (ALB) and [module 07](../07_cloudfront/README.md)
(CloudFront). floci running and `.env` loaded.

## Tests

[`../../tests/unit/test_18_multi_region.py`](../../tests/unit/test_18_multi_region.py)
checks that `build_stacks` creates the two regional stacks in two regions
(default and configured, and rejects equal regions), the service and ALB
in each region (listener ports, tags), the role and region in each task's
response, the rejected unknown role, the global stack appearing only once
both origins are set, and the distribution's origin group (origins,
timeouts, failover codes, allowed methods):

```bash
uv run pytest tests/unit/test_18_multi_region.py -v
```

## Deploy with floci (local, free)

```bash
uv run cdk list | grep MultiRegion          # the three stacks (.env.example sets both origins)
uv run cdk bootstrap aws://000000000000/us-east-1 aws://000000000000/us-west-2
uv run cdk deploy MultiRegionPrimaryStack MultiRegionSecondaryStack --require-approval never --method=direct
curl -s localhost:8094/ | grep Name          # Name: primary us-east-1
curl -s localhost:8099/ | grep Name          # Name: secondary us-west-2
uv run cdk deploy MultiRegionGlobalStack --require-approval never --method=direct
```

floci deploys the distribution but doesn't implement origin groups, so it
answers `502 No origin matched the request.` - see
[floci vs real AWS](#floci-vs-real-aws). The two regional stacks work
fully.

## Deploy to real AWS (optional)

Cost: **everything twice** - two NAT Gateways, two ALBs, two sets of
tasks - plus CloudFront requests. See [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/)
and [Amazon CloudFront pricing](https://aws.amazon.com/cloudfront/pricing/).

```bash
unset AWS_ENDPOINT_URL CDK_MULTI_REGION_PRIMARY_ORIGIN CDK_MULTI_REGION_SECONDARY_ORIGIN \
      CDK_MULTI_REGION_PRIMARY_ORIGIN_PORT CDK_MULTI_REGION_SECONDARY_ORIGIN_PORT \
      CDK_PORT_MULTI_REGION CDK_PORT_MULTI_REGION_SECONDARY
ACCOUNT=$(aws sts get-caller-identity --query Account --output text --profile <your-aws-cli-profile>)
uv run cdk bootstrap "aws://$ACCOUNT/us-east-1" "aws://$ACCOUNT/us-west-2" --profile <your-aws-cli-profile>

# 1. the two regions
uv run cdk deploy MultiRegionPrimaryStack MultiRegionSecondaryStack --profile <your-aws-cli-profile>

# 2. their ALB DNS names become the CloudFront origins
dns() { aws cloudformation describe-stacks --stack-name "$1" --region "$2" --profile <your-aws-cli-profile> \
  --query "Stacks[0].Outputs[?OutputKey=='AlbDnsName'].OutputValue" --output text; }
export CDK_MULTI_REGION_PRIMARY_ORIGIN=$(dns MultiRegionPrimaryStack us-east-1)
export CDK_MULTI_REGION_SECONDARY_ORIGIN=$(dns MultiRegionSecondaryStack us-west-2)
uv run cdk deploy MultiRegionGlobalStack --profile <your-aws-cli-profile>
```

The global stack takes the origins as variables rather than as CDK
cross-stack references: references across regions need the CDK's
`crossRegionReferences`, which the CDK documents as experimental.

## Verify

```bash
for r in us-east-1 us-west-2; do
  aws ecs describe-services --region "$r" --cluster learning-ecs-dev-ecs-multi-region \
    --services learning-ecs-dev-multi-region-web --query 'services[0].[serviceArn,runningCount]' --output text
done

DIST=$(aws cloudformation describe-stacks --stack-name MultiRegionGlobalStack \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" --output text)
aws cloudfront get-distribution-config --id "$DIST" \
  --query 'DistributionConfig.[Origins.Items[].[Id,DomainName,ConnectionAttempts,ConnectionTimeout],OriginGroups.Items[0].FailoverCriteria]'

# real AWS
curl -s "https://$(aws cloudfront get-distribution --id "$DIST" --query Distribution.DomainName --output text)/" | grep Name
# Name: primary us-east-1
```

The listing below is for the primary stack. The secondary stack has the
same resources: run the same commands with `STACK=MultiRegionSecondaryStack
REGION=us-west-2`. The global stack holds one `AWS::CloudFront::Distribution`
(`aws cloudfront get-distribution --id "$DIST"`).

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
`make cdk-resources STACK=MultiRegionPrimaryStack` runs the same commands for you.

```bash
# Match these to your deployment: CDK_PRODUCT and CDK_ENVIRONMENT in .env, and
# the region you deployed to (floci: the one in .env).
PRODUCT=learning-ecs ENV=dev REGION=us-east-1
STACK=MultiRegionPrimaryStack
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
aws ecs describe-clusters --clusters "${PRODUCT}-${ENV}-ecs-multi-region" --query "clusters[].[clusterName,status]" --output table --region "$REGION"
# AWS::IAM::Role (WebTaskDefinitionTaskRole2EE1C0E7)
aws iam get-role --role-name "$(pid WebTaskDefinitionTaskRole2EE1C0E7)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (WebTaskDefinition8DF7C630)
aws ecs describe-task-definition --task-definition "$(pid WebTaskDefinition8DF7C630)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (WebTaskDefinitionExecutionRole225B46C9)
aws iam get-role --role-name "$(pid WebTaskDefinitionExecutionRole225B46C9)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (WebLogGroup68B8CF3C)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/multi-region-web" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ECS::Service (WebService7F8A1763)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-multi-region" --services "${PRODUCT}-${ENV}-multi-region-web" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (WebServiceSecurityGroupE736A6BB)
aws ec2 describe-security-groups --group-ids "$(pid WebServiceSecurityGroupE736A6BB)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::LoadBalancer (Alb16C2F182)
aws elbv2 describe-load-balancers --load-balancer-arns "$(pid Alb16C2F182)" --query "LoadBalancers[].[LoadBalancerName,Type,State.Code]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (AlbSecurityGroup580F65A6)
aws ec2 describe-security-groups --group-ids "$(pid AlbSecurityGroup580F65A6)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::Listener (AlbHttp7966E42E)
aws elbv2 describe-listeners --listener-arns "$(pid AlbHttp7966E42E)" --query "Listeners[].[Port,Protocol]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::TargetGroup (AlbHttpWebGroupFB8A8200)
aws elbv2 describe-target-groups --target-group-arns "$(pid AlbHttpWebGroupFB8A8200)" --query "TargetGroups[].[TargetGroupName,Port,TargetType]" --output table --region "$REGION"
# Also created - listed in the table above:
#   11 VPC sub-resources (subnets, route tables, gateways, endpoints) - built by shared/network.py, listed one by one in modules/01_network/README.md
#   AWS::ECS::ClusterCapacityProviderAssociations Cluster3DA9CCBA - shown by its ECS cluster (describe-clusters --include ATTACHMENTS)
#   AWS::IAM::Policy WebTaskDefinitionTaskRoleDefaultPolicyD1A8300E - shown by its IAM role
#   AWS::IAM::Policy WebTaskDefinitionExecutionRoleDefaultPolicy5D8A2D6F - shown by its IAM role
#   AWS::EC2::SecurityGroupIngress WebServiceSecurityGroupfromMultiRegionPrimaryStackAlbSecurityGroup926B1B18802C7820CD - shown by its security group
#   AWS::EC2::SecurityGroupEgress AlbSecurityGrouptoMultiRegionPrimaryStackWebServiceSecurityGroupCF4F3F118095EE9700 - shown by its security group
```
<!-- END resource-commands -->

## Manage it with the AWS CLI

Run against floci while writing this module (except where noted).

### A regional failover drill

Simulate losing the primary region's application by scaling it to zero:
its ALB then answers `503` (no healthy targets), and CloudFront sends
the requests to the secondary region.

```bash
URL="https://$(aws cloudfront get-distribution --id "$DIST" --query Distribution.DomainName --output text)/"

aws ecs update-service --region us-east-1 --cluster learning-ecs-dev-ecs-multi-region \
  --service learning-ecs-dev-multi-region-web --desired-count 0
aws ecs wait services-stable --region us-east-1 --cluster learning-ecs-dev-ecs-multi-region \
  --services learning-ecs-dev-multi-region-web
curl -s -o /dev/null -w 'primary ALB: %{http_code}\n' "http://$(dns MultiRegionPrimaryStack us-east-1)/"   # 503 (floci: localhost:8094)

curl -s "$URL" | grep Name          # real AWS: Name: secondary us-west-2

# back to normal - the next request goes to the primary again
aws ecs update-service --region us-east-1 --cluster learning-ecs-dev-ecs-multi-region \
  --service learning-ecs-dev-multi-region-web --desired-count 2
```

A real regional outage is broader than a missing service (the ALB itself,
the network, AWS APIs in that region). For controlled experiments with
real faults, see [AWS Fault Injection Service](https://docs.aws.amazon.com/fis/latest/userguide/what-is.html).

### Operating two regions

```bash
# every command takes --region; a loop keeps both regions in step
for r in us-east-1 us-west-2; do
  echo "== $r"
  aws ecs describe-services --region "$r" --cluster learning-ecs-dev-ecs-multi-region \
    --services learning-ecs-dev-multi-region-web \
    --query 'services[0].[taskDefinition,desiredCount,runningCount,deployments[0].rolloutState]' --output text
done

# ship the same change to both regions, secondary first (smaller blast radius)
uv run cdk deploy MultiRegionSecondaryStack
uv run cdk deploy MultiRegionPrimaryStack

# compare what is deployed in each region
uv run cdk diff MultiRegionPrimaryStack MultiRegionSecondaryStack
```

### Tune the failover

On real AWS (floci doesn't store the origin group):

```bash
aws cloudfront get-distribution-config --id "$DIST" > dist.json
ETAG=$(jq -r .ETag dist.json)
# e.g. also fail over on 429 (throttling) - edit, then send back the whole config
jq '.DistributionConfig | .OriginGroups.Items[0].FailoverCriteria.StatusCodes = {"Quantity":5,"Items":[429,500,502,503,504]}' \
  dist.json > dist-new.json
aws cloudfront update-distribution --id "$DIST" --if-match "$ETAG" --distribution-config file://dist-new.json
aws cloudfront wait distribution-deployed --id "$DIST"
```

Prefer changing [`stack.py`](stack.py) (or the `CDK_MULTI_REGION_*`
variables) and `cdk deploy`, so the stack does not drift.

## Metrics to watch

| Namespace (region) | Metric | Dimensions | Watch for |
|---|---|---|---|
| `AWS/CloudFront` (us-east-1) | `Requests`, `5xxErrorRate`, `OriginLatency` | `DistributionId`, `Region=Global` | what viewers see; `OriginLatency` needs additional metrics enabled |
| `AWS/ApplicationELB` (each region) | `RequestCount` | `LoadBalancer` | traffic arriving at the secondary = failovers happening |
| `AWS/ApplicationELB` (each region) | `HTTPCode_ELB_5XX_Count`, `HealthyHostCount` | `LoadBalancer` / `TargetGroup` | why the primary failed |
| `AWS/ECS` / `ECS/ContainerInsights` (each region) | `CPUUtilization`, `RunningTaskCount` | `ClusterName`, `ServiceName` | the secondary must be able to absorb the primary's load |

CloudWatch metrics are **regional**: build one dashboard with widgets from
both regions (a widget's `region` property), or use cross-account
cross-region observability - see [module 19](../19_cloudwatch/README.md).

```bash
for r in us-east-1 us-west-2; do
  LB=$(aws elbv2 describe-load-balancers --region "$r" --query \
    "LoadBalancers[?starts_with(LoadBalancerName,'learning-ecs-dev-alb-mult')].LoadBalancerArn | [0]" --output text)
  echo "$r: $(aws cloudwatch get-metric-statistics --region "$r" --namespace AWS/ApplicationELB --metric-name RequestCount \
    --dimensions Name=LoadBalancer,Value="${LB#*:loadbalancer/}" --statistics Sum --period 3600 \
    --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --query 'Datapoints[0].Sum' --output text)"
done
```

## Troubleshooting

| Symptom | Where to look | Typical cause / fix |
|---|---|---|
| `cdk deploy` fails in the second region: not bootstrapped | the error message | `cdk bootstrap aws://<account>/<region>` for **each** region |
| `MultiRegionGlobalStack` missing from `cdk list` | your shell | `CDK_MULTI_REGION_PRIMARY_ORIGIN` and `..._SECONDARY_ORIGIN` must both be set |
| No failover although the primary is down | the request method, the failover codes | only GET/HEAD/OPTIONS fail over; the primary's error code isn't in the list |
| Every request slow while the primary is down | origin timeouts | each request still tries the primary first - lower connection attempts/timeout |
| Secondary overloaded after failover | its scaling | size it (or let it scale - [module 16](../16_autoscaling/README.md)) for the primary's traffic |
| `502` from CloudFront with both regions up | origin protocol/port | the origins are HTTP on the listener port; check `CDK_MULTI_REGION_*_ORIGIN_PORT` |

More: [`../../docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md).

## floci vs real AWS

| Behavior | floci 2.1.0 | Real AWS |
|---|---|---|
| Stacks in two regions (bootstrap, ECS, ALB per region) | work | work |
| ALB listeners of both regions | on floci's container: distinct ports (`8094`, `8099`) | each ALB has its own addresses |
| CloudFront origins `*.elb.floci` | not resolvable - origins are `localhost` (`.env.example`) | the ALB DNS names |
| CloudFront **origin groups** | **not stored** (from CloudFormation or `update-distribution`); requests get `502 No origin matched the request.` | failover as described |
| Origin `ConnectionAttempts` / `ConnectionTimeout` | not reflected (shows 3 / 10) | applied |
| Metrics | not produced | produced, per region |

## Clean up

```bash
uv run cdk destroy MultiRegionGlobalStack MultiRegionSecondaryStack MultiRegionPrimaryStack
uv run python scripts/floci_prune.py --apply --region us-east-1 --region us-west-2   # floci only
rm -f dist.json dist-new.json
```

## Notes and cautions

- **Cost**: the whole stack twice. A cheaper "pilot light" keeps the
  secondary at minimum size (or `desired-count 0`) and scales it during a
  failover - at the price of a longer RTO.
- **Test the failover regularly** - an untested secondary region is a
  hope, not a plan.
- **Quotas are per region**: Fargate vCPU, ALBs, Elastic IPs... the
  secondary must be able to run the primary's peak.
- **Images**: Docker Hub serves both regions here. With ECR, use
  [replication](https://docs.aws.amazon.com/AmazonECR/latest/userguide/replication.html)
  so each region pulls locally.
- CloudFront requires certificates for custom domain names in `us-east-1`.

## References

- [AWS CDK - Environments](https://docs.aws.amazon.com/cdk/v2/guide/environments.html) · [Bootstrapping](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping.html)
- [Optimize high availability with CloudFront origin failover](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/high_availability_origin_failover.html)
- [AWS CDK `aws_cloudfront_origins` README - Failover Origins](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_cloudfront_origins/README.html)
- [Disaster recovery of workloads on AWS (whitepaper)](https://docs.aws.amazon.com/whitepapers/latest/disaster-recovery-workloads-on-aws/disaster-recovery-workloads-on-aws.html)
- [Docker Hub - traefik/whoami](https://hub.docker.com/r/traefik/whoami)
