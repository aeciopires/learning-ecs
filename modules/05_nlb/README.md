<!-- TOC -->

- [Module 05 - NLB (internet-facing and internal Network Load Balancers)](#module-05---nlb-internet-facing-and-internal-network-load-balancers)
  - [Overview](#overview)
  - [What you will learn](#what-you-will-learn)
  - [Architecture](#architecture)
  - [ALB or NLB?](#alb-or-nlb)
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

# Module 05 - NLB (internet-facing and internal Network Load Balancers)

## Overview

A **Network Load Balancer** (NLB) balances **TCP, UDP and TLS
connections** (layer 4) instead of HTTP requests. It doesn't look inside
the traffic - no path or header rules - but it handles very high
connection rates, keeps long-lived connections, gives you one static IP
per Availability Zone, is what an **API Gateway VPC link V1** points at
([module 06](../06_api_gateway/README.md)), and is the usual way to publish
a service through AWS PrivateLink.

Like module 04, this module runs two `traefik/whoami` services, one behind
an **internet-facing** NLB and one behind an **internal** NLB. Each NLB
gets its own **security group**, and each service's tasks only accept
traffic from their NLB's security group.

## What you will learn

- NLB listeners and target groups (TCP), with an HTTP health check on a
  TCP target group.
- Security groups on an NLB, and how the task security group trusts it.
- Cross-zone load balancing (off by default on NLBs - on here, configurable).
- Client IP preservation: off by default for `ip` targets over TCP (your
  task sees the NLB's address), how to turn it on, and proxy protocol v2.
- The `AWS/NetworkELB` metrics that matter (flows, resets, port
  allocation errors).

## Architecture

```
 internet ──TCP──> NLB learning-ecs-dev-nlb-public  (public subnets, SG: 0.0.0.0/0 on the listener port)
                     listener TCP :80 -> TG learning-ecs-dev-tg-nlb-public (TCP, ip, HTTP health check /health)
                                         -> nlb-public tasks (task SG: only from the NLB's SG, port 80)

 VPC ──TCP──> NLB learning-ecs-dev-nlb-internal (private subnets, SG: VPC CIDR only)
                 listener TCP :80 -> TG learning-ecs-dev-tg-nlb-internal -> nlb-internal tasks
```

## ALB or NLB?

| | ALB ([module 04](../04_alb/README.md)) | NLB (this module) |
|---|---|---|
| Layer | 7 (HTTP/HTTPS/gRPC) | 4 (TCP/UDP/TLS) |
| Routing | path, host, header, query, method | port only |
| Fixed responses, redirects, authentication | yes | no |
| Static IP per AZ / Elastic IPs | no | yes |
| Client IP at the target | `X-Forwarded-For` header | source IP (with client IP preservation) or proxy protocol v2 |
| API Gateway VPC link V1 (REST APIs), PrivateLink endpoint service | no (VPC link V2 only) | yes |
| Non-HTTP protocols (databases, MQTT, gRPC over raw TCP, UDP) | no | yes |

## AWS services and CDK constructs used

| AWS service | CDK construct (Python) | Level |
|---|---|---|
| Elastic Load Balancing | `aws_cdk.aws_elasticloadbalancingv2.NetworkLoadBalancer` | L2 |
| Elastic Load Balancing | `NetworkListener.add_targets`, `HealthCheck` | L2 |
| Amazon VPC | `aws_cdk.aws_ec2.SecurityGroup` (one per NLB) | L2 |
| Amazon ECS | `aws_cdk.aws_ecs.FargateService` (via `shared/ecs.py`) | L2 |

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CDK_NLB_IMAGE` | `traefik/whoami:v1.12.0` | image of both services |
| `CDK_NLB_DESIRED_COUNT` | `CDK_DESIRED_COUNT` (`2`) | tasks per service |
| `CDK_NLB_CROSS_ZONE` | `true` | cross-zone load balancing on both NLBs |
| `CDK_PORT_NLB_PUBLIC` / `CDK_PORT_NLB_INTERNAL` | `80` (`.env.example`: `8083` / `8084`) | listener ports |

## Prerequisites

[Module 04](../04_alb/README.md) (load balancer basics, target groups,
health checks). floci running and `.env` loaded.

## Tests

[`../../tests/unit/test_05_nlb.py`](../../tests/unit/test_05_nlb.py) checks
the two `network` load balancers (scheme, name, security group, cross-zone
on/off), the TCP listeners and configurable ports, TCP target groups with an
HTTP health check, the NLB security groups (public: `0.0.0.0/0`; internal:
the VPC CIDR), the task ingress rule that only trusts the NLB security
groups, and the mandatory tags:

```bash
uv run pytest tests/unit/test_05_nlb.py -v
```

## Deploy with floci (local, free)

```bash
uv run cdk bootstrap
uv run cdk synth NlbStack
uv run cdk diff NlbStack
uv run cdk deploy NlbStack --require-approval never --method=direct
```

## Deploy to real AWS (optional)

**Each NLB bills per hour plus per NLCU** - see
[Elastic Load Balancing pricing](https://aws.amazon.com/elasticloadbalancing/pricing/);
cross-zone traffic also incurs data transfer charges.

```bash
unset AWS_ENDPOINT_URL CDK_PORT_NLB_PUBLIC CDK_PORT_NLB_INTERNAL
uv run cdk bootstrap --profile <your-aws-cli-profile>
uv run cdk diff NlbStack --profile <your-aws-cli-profile>
uv run cdk deploy NlbStack --profile <your-aws-cli-profile>
```

## Verify

```bash
ENDPOINT=$(aws cloudformation describe-stacks --stack-name NlbStack \
  --query "Stacks[0].Outputs[?OutputKey=='PublicEndpoint'].OutputValue" --output text)
[ -n "${AWS_ENDPOINT_URL:-}" ] && ENDPOINT=localhost:${CDK_PORT_NLB_PUBLIC:-8083}   # floci

for i in 1 2 3; do curl -s "http://$ENDPOINT/" | grep -E '^(Name|Hostname|RemoteAddr)'; done
# RemoteAddr is the NLB's address, not yours: client IP preservation is off by default for ip targets over TCP.

TG=$(aws elbv2 describe-target-groups --names learning-ecs-dev-tg-nlb-public --query 'TargetGroups[0].TargetGroupArn' --output text)
aws elbv2 describe-target-health --target-group-arn "$TG" \
  --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State]' --output table

# The internal NLB, from inside the network (floci) - see module 04 for the same check on real AWS:
INTERNAL=$(aws cloudformation describe-stacks --stack-name NlbStack \
  --query "Stacks[0].Outputs[?OutputKey=='InternalEndpoint'].OutputValue" --output text)
FLOCI_IP=$(docker inspect learning-ecs-floci --format '{{(index .NetworkSettings.Networks "learning-ecs-floci-net").IPAddress}}')
docker run --rm --network learning-ecs-floci-net --dns "$FLOCI_IP" curlimages/curl:8.22.0 -s "http://$INTERNAL/" | grep '^Name'
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
`make cdk-resources STACK=NlbStack` runs the same commands for you.

```bash
# Match these to your deployment: CDK_PRODUCT and CDK_ENVIRONMENT in .env, and
# the region you deployed to (floci: the one in .env).
PRODUCT=learning-ecs ENV=dev REGION=us-east-1
STACK=NlbStack
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
aws ecs describe-clusters --clusters "${PRODUCT}-${ENV}-ecs-nlb" --query "clusters[].[clusterName,status]" --output table --region "$REGION"
# AWS::IAM::Role (PublicTaskDefinitionTaskRole8D79C7A5)
aws iam get-role --role-name "$(pid PublicTaskDefinitionTaskRole8D79C7A5)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (PublicTaskDefinition83EAACAD)
aws ecs describe-task-definition --task-definition "$(pid PublicTaskDefinition83EAACAD)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (PublicTaskDefinitionExecutionRole9BBC2AA3)
aws iam get-role --role-name "$(pid PublicTaskDefinitionExecutionRole9BBC2AA3)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (PublicLogGroup43F4974E)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/nlb-public" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ECS::Service (PublicService03EEB7D8)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-nlb" --services "${PRODUCT}-${ENV}-nlb-public" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (PublicServiceSecurityGroupDF5A4D68)
aws ec2 describe-security-groups --group-ids "$(pid PublicServiceSecurityGroupDF5A4D68)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::IAM::Role (InternalTaskDefinitionTaskRoleE50E3A35)
aws iam get-role --role-name "$(pid InternalTaskDefinitionTaskRoleE50E3A35)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (InternalTaskDefinitionB18C5C24)
aws ecs describe-task-definition --task-definition "$(pid InternalTaskDefinitionB18C5C24)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (InternalTaskDefinitionExecutionRole94E0AF31)
aws iam get-role --role-name "$(pid InternalTaskDefinitionExecutionRole94E0AF31)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (InternalLogGroupA39D3796)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/nlb-internal" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ECS::Service (InternalService66F9E794)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-nlb" --services "${PRODUCT}-${ENV}-nlb-internal" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (InternalServiceSecurityGroup774CE209)
aws ec2 describe-security-groups --group-ids "$(pid InternalServiceSecurityGroup774CE209)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (PublicNlbSecurityGroupBA858A24)
aws ec2 describe-security-groups --group-ids "$(pid PublicNlbSecurityGroupBA858A24)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::LoadBalancer (PublicNlb09A969CF)
aws elbv2 describe-load-balancers --load-balancer-arns "$(pid PublicNlb09A969CF)" --query "LoadBalancers[].[LoadBalancerName,Type,State.Code]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::Listener (PublicNlbTcpB2C282E0)
aws elbv2 describe-listeners --listener-arns "$(pid PublicNlbTcpB2C282E0)" --query "Listeners[].[Port,Protocol]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::TargetGroup (PublicNlbTcpTasksGroup52A82023)
aws elbv2 describe-target-groups --target-group-arns "$(pid PublicNlbTcpTasksGroup52A82023)" --query "TargetGroups[].[TargetGroupName,Port,TargetType]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (InternalNlbSecurityGroupE90F6F92)
aws ec2 describe-security-groups --group-ids "$(pid InternalNlbSecurityGroupE90F6F92)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::LoadBalancer (InternalNlbFEE15427)
aws elbv2 describe-load-balancers --load-balancer-arns "$(pid InternalNlbFEE15427)" --query "LoadBalancers[].[LoadBalancerName,Type,State.Code]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::Listener (InternalNlbTcpFB019BCB)
aws elbv2 describe-listeners --listener-arns "$(pid InternalNlbTcpFB019BCB)" --query "Listeners[].[Port,Protocol]" --output table --region "$REGION"
# AWS::ElasticLoadBalancingV2::TargetGroup (InternalNlbTcpTasksGroup0384D39C)
aws elbv2 describe-target-groups --target-group-arns "$(pid InternalNlbTcpTasksGroup0384D39C)" --query "TargetGroups[].[TargetGroupName,Port,TargetType]" --output table --region "$REGION"
# Also created - listed in the table above:
#   11 VPC sub-resources (subnets, route tables, gateways, endpoints) - built by shared/network.py, listed one by one in modules/01_network/README.md
#   AWS::ECS::ClusterCapacityProviderAssociations Cluster3DA9CCBA - shown by its ECS cluster (describe-clusters --include ATTACHMENTS)
#   AWS::IAM::Policy PublicTaskDefinitionTaskRoleDefaultPolicyA711E30F - shown by its IAM role
#   AWS::IAM::Policy PublicTaskDefinitionExecutionRoleDefaultPolicy7B0C6B7F - shown by its IAM role
#   AWS::EC2::SecurityGroupIngress PublicServiceSecurityGroupfromNlbStackPublicNlbSecurityGroup57E5C03880187D6150 - shown by its security group
#   AWS::IAM::Policy InternalTaskDefinitionTaskRoleDefaultPolicyF8A4882F - shown by its IAM role
#   AWS::IAM::Policy InternalTaskDefinitionExecutionRoleDefaultPolicyCD3A7EE9 - shown by its IAM role
#   AWS::EC2::SecurityGroupIngress InternalServiceSecurityGroupfromNlbStackInternalNlbSecurityGroup0F0AC71F80980E9488 - shown by its security group
#   AWS::EC2::SecurityGroupEgress PublicNlbSecurityGrouptoNlbStackPublicServiceSecurityGroup9A217907803938D8FB - shown by its security group
#   AWS::EC2::SecurityGroupEgress InternalNlbSecurityGrouptoNlbStackInternalServiceSecurityGroupDDB4B4B98022E7FC16 - shown by its security group
```
<!-- END resource-commands -->

## Manage it with the AWS CLI

Run against floci while writing this module. Creating an NLB from scratch
is the same flow as the ALB in [module 04](../04_alb/README.md#an-alb-for-an-ecs-service-from-scratch)
with `--type network`, a `TCP` target group and a `TCP` listener:

```bash
aws elbv2 create-load-balancer --name <name> --type network --scheme internal \
  --subnets <subnet-a> <subnet-b> --security-groups <nlb-sg>
aws elbv2 create-target-group --name <tg-name> --protocol TCP --port 80 --vpc-id <vpc-id> --target-type ip \
  --health-check-protocol HTTP --health-check-path /health
aws elbv2 create-listener --load-balancer-arn <nlb-arn> --protocol TCP --port 80 \
  --default-actions Type=forward,TargetGroupArn=<tg-arn>
```

The NLB-specific settings:

```bash
LB=$(aws elbv2 describe-load-balancers --names learning-ecs-dev-nlb-public --query 'LoadBalancers[0].LoadBalancerArn' --output text)
TG=$(aws elbv2 describe-target-groups --names learning-ecs-dev-tg-nlb-public --query 'TargetGroups[0].TargetGroupArn' --output text)

# cross-zone load balancing (load balancer level; a target group can override it)
aws elbv2 modify-load-balancer-attributes --load-balancer-arn "$LB" \
  --attributes Key=load_balancing.cross_zone.enabled,Value=true

# client IP preservation, deregistration delay, proxy protocol v2 (target group level)
aws elbv2 modify-target-group-attributes --target-group-arn "$TG" \
  --attributes Key=preserve_client_ip.enabled,Value=true Key=deregistration_delay.timeout_seconds,Value=60
aws elbv2 modify-target-group-attributes --target-group-arn "$TG" \
  --attributes Key=proxy_protocol_v2.enabled,Value=false   # true only if the app parses the PROXY v2 header
aws elbv2 describe-target-group-attributes --target-group-arn "$TG" --output table

# security groups of the NLB (replaces the list)
SG=$(aws elbv2 describe-load-balancers --load-balancer-arns "$LB" --query 'LoadBalancers[0].SecurityGroups[0]' --output text)
aws elbv2 set-security-groups --load-balancer-arn "$LB" --security-groups "$SG"
```

With client IP preservation on, the task sees the *client's* address. The
task security group can keep trusting the NLB's security group: AWS
documents that a rule referencing the NLB's security group keeps accepting
traffic through the NLB even with client IP preservation enabled.

## Metrics to watch

`AWS/NetworkELB` (60-second datapoints; dimension `LoadBalancer=net/<name>/<id>`,
`TargetGroup=targetgroup/<name>/<id>`):

| Metric | Statistic | What it tells you |
|---|---|---|
| `ActiveFlowCount` / `NewFlowCount` | Average / Sum | concurrent and new TCP connections |
| `ProcessedBytes` | Sum | throughput |
| `HealthyHostCount` / `UnHealthyHostCount` | Maximum / Minimum | AWS recommends alarming when the maximum healthy count drops below your minimum, and when the minimum unhealthy count rises above 0 |
| `TCP_Target_Reset_Count` / `TCP_Client_Reset_Count` / `TCP_ELB_Reset_Count` | Sum | who is resetting connections |
| `PortAllocationErrorCount` | Sum | with client IP preservation off, each target accepts about 55,000 simultaneous connections from the NLB - add tasks if this is non-zero |
| `SecurityGroupBlockedFlowCount_Inbound_TCP` | Sum | flows the NLB's security group rejected |

```bash
LB_DIM=$(echo "$LB" | cut -d/ -f2-)
aws cloudwatch get-metric-statistics --namespace AWS/NetworkELB --metric-name ActiveFlowCount \
  --dimensions Name=LoadBalancer,Value="$LB_DIM" \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Average Maximum --output table
```

## Troubleshooting

| Symptom | Where to look | Typical cause / fix |
|---|---|---|
| Connections time out | `SecurityGroupBlockedFlowCount_Inbound_TCP`, security groups | the NLB's security group doesn't allow the client, or the task's security group doesn't reference the NLB's |
| Targets stay `unhealthy` | `describe-target-health`, health check settings | health check port/path wrong; health checks are subject to the NLB security group's **outbound** rules (this module allows port 80 to the VPC) and to the task's inbound rules |
| App logs show the NLB's IP, not the client's | target group attributes | expected for `ip` targets over TCP - enable `preserve_client_ip.enabled` or proxy protocol v2 |
| Connection errors from a task calling its own internal NLB | client IP preservation | "NAT loopback" isn't supported with client IP preservation on; disable it or call the service another way (Service Connect) |
| Traffic only to one AZ's tasks | cross-zone setting | cross-zone is off by default on NLBs; enable it, or keep enough tasks in every AZ |

More: [`../../docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md) and
[Troubleshoot your Network Load Balancer](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-troubleshooting.html).

## floci vs real AWS

| Behavior | floci 2.1.0 | Real AWS |
|---|---|---|
| NLB, TCP listeners, target groups, ECS target registration, HTTP health checks | created and working; connections are balanced across tasks | same |
| Listener address | floci's container, one unique port per listener (`CDK_PORT_*`) | one IP per AZ (static), DNS name per NLB |
| Security groups on the NLB, `internal` scheme | stored; **not enforced** | enforced |
| Source address seen by the task | floci's container IP | the NLB's private IP (default) or the client IP |
| Attributes from CloudFormation (cross-zone, ...) | ignored - `modify-*-attributes` from the CLI works | applied |
| `AWS/NetworkELB` metrics | not produced | produced |

## Clean up

```bash
uv run cdk destroy NlbStack
uv run python scripts/floci_prune.py --apply   # floci only
```

## Notes and cautions

- **Cost**: see [Deploy to real AWS](#deploy-to-real-aws-optional).
- **Security groups on NLBs must be associated when the NLB is created** -
  an NLB created without any can't get one later (it can change them
  afterwards if it had at least one). This module always creates them. See
  [Update the security groups for your Network Load Balancer](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-security-groups.html).
- **Deregistration delay** defaults to 300 s on AWS (AWS recommends at
  least 120 s); this module uses 30 s so deployments finish quickly while
  you learn.

## References

- [Use a Network Load Balancer for Amazon ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/nlb.html)
- [What is a Network Load Balancer?](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/introduction.html)
- [Edit target group attributes for your Network Load Balancer (client IP preservation, proxy protocol, deregistration)](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/edit-target-group-attributes.html)
- [Security groups for your Network Load Balancer](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-security-groups.html)
- [CloudWatch metrics for your Network Load Balancer](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-cloudwatch-metrics.html)
- [Troubleshoot your Network Load Balancer](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/load-balancer-troubleshooting.html)
- [AWS CDK API Reference (Python) - `aws_cdk.aws_elasticloadbalancingv2.NetworkLoadBalancer`](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_elasticloadbalancingv2/NetworkLoadBalancer.html)
