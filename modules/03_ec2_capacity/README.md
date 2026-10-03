<!-- TOC -->

- [Module 03 - EC2 capacity (Auto Scaling group capacity provider, placement, daemons)](#module-03---ec2-capacity-auto-scaling-group-capacity-provider-placement-daemons)
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
  - [Fargate or EC2?](#fargate-or-ec2)
  - [Metrics to watch](#metrics-to-watch)
  - [Troubleshooting](#troubleshooting)
  - [floci vs real AWS](#floci-vs-real-aws)
  - [Clean up](#clean-up)
  - [Notes and cautions](#notes-and-cautions)
  - [References](#references)

<!-- TOC -->

# Module 03 - EC2 capacity (Auto Scaling group capacity provider, placement, daemons)

## Overview

With Fargate ([module 02](../02_fargate_service/README.md)) AWS runs the
servers. With the **EC2 launch type** you bring the servers - EC2
instances running the Amazon ECS container agent - and ECS places tasks on
them. At large scale this is often cheaper per vCPU, gives you instance
types Fargate doesn't offer (GPUs, very large memory, local NVMe), and
lets you run per-host agents.

This module builds that setup the way AWS recommends today:

- a **launch template** with the Amazon ECS-optimized Amazon Linux 2023 AMI;
- an **Auto Scaling group** (ASG) of those instances in the private subnets;
- an **ASG capacity provider** with **managed scaling** - ECS itself grows
  and shrinks the ASG to fit the tasks it needs to place - and managed
  instance draining;
- a **REPLICA** service (nginx) with **placement strategies**;
- a **DAEMON** service ([Prometheus node-exporter](https://hub.docker.com/r/prom/node-exporter))
  that runs exactly one task on every instance.

## What you will learn

- How an EC2 instance joins a cluster (ECS-optimized AMI + the
  `ECS_CLUSTER=` line the capacity provider adds to the user data + the
  container instance IAM role).
- Managed scaling: the `CapacityProviderReservation` metric and
  `targetCapacity` (100% = no spare instances; below 100% = headroom).
- Placement strategies (`spread`, `binpack`, `random`) and constraints.
- REPLICA vs DAEMON scheduling.
- Draining an instance before maintenance.
- When to pick EC2 over Fargate (see [Fargate or EC2?](#fargate-or-ec2)).

## Architecture

```
 ECS cluster learning-ecs-dev-ecs-ec2
   capacity provider learning-ecs-dev-ec2-asg-cp  (managed scaling, target CDK_EC2_TARGET_CAPACITY %)
        |
        v  sets DesiredCapacity
   Auto Scaling group learning-ecs-dev-ec2-asg  (min CDK_EC2_MIN_CAPACITY .. max CDK_EC2_MAX_CAPACITY)
   +---- instance (AZ a) -------------------+   +---- instance (AZ b) ------------------+
   | ECS agent                               |   | ECS agent                              |
   | node-exporter (DAEMON, host network)    |   | node-exporter (DAEMON, host network)   |
   | web task (REPLICA, awsvpc ENI)          |   | web task (REPLICA, awsvpc ENI)         |
   +-----------------------------------------+   +----------------------------------------+
   web placement: spread(attribute:ecs.availability-zone), then binpack(memory)
```

## AWS services and CDK constructs used

| AWS service | CDK construct (Python) | Level |
|---|---|---|
| Amazon EC2 | `aws_cdk.aws_ec2.LaunchTemplate`, `aws_cdk.aws_ecs.EcsOptimizedImage.amazon_linux2023()` | L2 |
| Amazon EC2 Auto Scaling | `aws_cdk.aws_autoscaling.AutoScalingGroup` | L2 |
| Amazon ECS | `aws_cdk.aws_ecs.AsgCapacityProvider`, `Cluster.add_asg_capacity_provider` | L2 |
| Amazon ECS | `aws_cdk.aws_ecs.Ec2TaskDefinition`, `aws_cdk.aws_ecs.Ec2Service` (`daemon=True`) | L2 |
| Amazon ECS | `aws_cdk.aws_ecs.PlacementStrategy` | L2 (helper) |
| AWS IAM | `aws_cdk.aws_iam.Role` (container instance role) | L2 |

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CDK_EC2_INSTANCE_TYPE` | `t3.small` | instance type of the launch template |
| `CDK_EC2_MIN_CAPACITY` / `CDK_EC2_MAX_CAPACITY` | `1` / `4` | ASG bounds (managed scaling never changes them) |
| `CDK_EC2_TARGET_CAPACITY` | `100` | managed scaling target, percent |
| `CDK_EC2_WARMUP_SECONDS` | `300` | instance warm-up period for managed scaling |
| `CDK_EC2_TERMINATION_PROTECTION` | `false` | managed termination protection (see [Notes](#notes-and-cautions)) |
| `CDK_EC2_DESIRED_COUNT` | `CDK_DESIRED_COUNT` (`2`) | web tasks |
| `CDK_EC2_WEB_IMAGE` / `CDK_EC2_DAEMON_IMAGE` | `nginx:1.30-alpine` / `prom/node-exporter:v1.9.1` | Docker Hub images |

## Prerequisites

- [Module 02](../02_fargate_service/README.md) done (task definitions, services, day-2 commands).
- floci running and `.env` loaded (see [`../../REQUIREMENTS.md`](../../REQUIREMENTS.md)).

## Tests

[`../../tests/unit/test_03_ec2_capacity.py`](../../tests/unit/test_03_ec2_capacity.py)
checks the launch template (AL2023 ECS-optimized AMI parameter, IMDSv2,
instance type), the instance role's managed policy, that ASG size and the
capacity provider's managed scaling/draining follow their variables, the
placement strategies of the web service, the DAEMON service on the host
network, and the mandatory tags:

```bash
uv run pytest tests/unit/test_03_ec2_capacity.py -v
```

## Deploy with floci (local, free)

```bash
uv run cdk bootstrap
uv run cdk synth Ec2CapacityStack
uv run cdk diff Ec2CapacityStack
uv run cdk deploy Ec2CapacityStack --require-approval never --method=direct
```

Read [floci vs real AWS](#floci-vs-real-aws) first: floci accepts every
resource here, but it doesn't launch EC2 instances - it runs the tasks
directly as Docker containers.

## Deploy to real AWS (optional)

**EC2 instances, the NAT Gateway and EBS volumes bill hourly** - see
[Amazon EC2 pricing](https://aws.amazon.com/ec2/pricing/). With the
defaults, one to four `t3.small` instances run until you destroy the stack.

```bash
unset AWS_ENDPOINT_URL
uv run cdk bootstrap --profile <your-aws-cli-profile>
uv run cdk diff Ec2CapacityStack --profile <your-aws-cli-profile>
uv run cdk deploy Ec2CapacityStack --profile <your-aws-cli-profile>
```

## Verify

```bash
CLUSTER=learning-ecs-dev-ecs-ec2

# Instances that joined the cluster (real AWS): status, AZ, free CPU/memory
aws ecs list-container-instances --cluster "$CLUSTER"
aws ecs describe-container-instances --cluster "$CLUSTER" \
  --container-instances $(aws ecs list-container-instances --cluster "$CLUSTER" --query containerInstanceArns --output text) \
  --query "containerInstances[].[ec2InstanceId,status,runningTasksCount,remainingResources[?name=='CPU'].integerValue|[0],remainingResources[?name=='MEMORY'].integerValue|[0]]" \
  --output table

# Both services, and the daemon's scheduling strategy
aws ecs describe-services --cluster "$CLUSTER" \
  --services learning-ecs-dev-ec2-web learning-ecs-dev-ec2-node-exporter \
  --query "services[].[serviceName,schedulingStrategy,desiredCount,runningCount]" --output table

# The capacity provider and the ASG it manages
aws ecs describe-capacity-providers --capacity-providers learning-ecs-dev-ec2-asg-cp \
  --query "capacityProviders[0].autoScalingGroupProvider"
aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names learning-ecs-dev-ec2-asg \
  --query "AutoScalingGroups[0].[MinSize,DesiredCapacity,MaxSize,length(Instances)]"

# The target tracking policy ECS created on the ASG (real AWS)
aws autoscaling describe-policies --auto-scaling-group-name learning-ecs-dev-ec2-asg \
  --query "ScalingPolicies[].[PolicyName,PolicyType,TargetTrackingConfiguration.TargetValue]"
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
`make cdk-resources STACK=Ec2CapacityStack` runs the same commands for you.

```bash
# Match these to your deployment: CDK_PRODUCT and CDK_ENVIRONMENT in .env, and
# the region you deployed to (floci: the one in .env).
PRODUCT=learning-ecs ENV=dev REGION=us-east-1
STACK=Ec2CapacityStack
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
aws ecs describe-clusters --clusters "${PRODUCT}-${ENV}-ecs-ec2" --query "clusters[].[clusterName,status]" --output table --region "$REGION"
# AWS::IAM::Role (InstanceRole3CCE2F1D)
aws iam get-role --role-name "$(pid InstanceRole3CCE2F1D)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::IAM::InstanceProfile (LaunchTemplateProfile94AA77CE)
aws iam get-instance-profile --instance-profile-name "$(pid LaunchTemplateProfile94AA77CE)" --query "InstanceProfile.[InstanceProfileName,Arn]" --output table --region "$REGION"
# AWS::EC2::LaunchTemplate (LaunchTemplate04EC5460)
aws ec2 describe-launch-templates --launch-template-ids "$(pid LaunchTemplate04EC5460)" --query "LaunchTemplates[].[LaunchTemplateId,LaunchTemplateName,LatestVersionNumber]" --output table --region "$REGION"
# AWS::AutoScaling::AutoScalingGroup (AutoScalingGroupASG804C35BE)
aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "${PRODUCT}-${ENV}-ec2-asg" --query "AutoScalingGroups[].[AutoScalingGroupName,MinSize,DesiredCapacity,MaxSize]" --output table --region "$REGION"
# AWS::ECS::CapacityProvider (CapacityProviderF92AC12E)
aws ecs describe-capacity-providers --capacity-providers "${PRODUCT}-${ENV}-ec2-asg-cp" --query "capacityProviders[].[name,status]" --output table --region "$REGION"
# AWS::IAM::Role (WebTaskDefinitionTaskRole2EE1C0E7)
aws iam get-role --role-name "$(pid WebTaskDefinitionTaskRole2EE1C0E7)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (WebTaskDefinition8DF7C630)
aws ecs describe-task-definition --task-definition "$(pid WebTaskDefinition8DF7C630)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (WebTaskDefinitionExecutionRole225B46C9)
aws iam get-role --role-name "$(pid WebTaskDefinitionExecutionRole225B46C9)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (WebLogGroup68B8CF3C)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/ec2-web" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ECS::Service (WebService7F8A1763)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-ec2" --services "${PRODUCT}-${ENV}-ec2-web" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
# AWS::EC2::SecurityGroup (WebServiceSecurityGroupE736A6BB)
aws ec2 describe-security-groups --group-ids "$(pid WebServiceSecurityGroupE736A6BB)" --query "SecurityGroups[].[GroupId,GroupName,VpcId]" --output table --region "$REGION"
# AWS::IAM::Role (DaemonTaskDefinitionTaskRoleCA400B60)
aws iam get-role --role-name "$(pid DaemonTaskDefinitionTaskRoleCA400B60)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (DaemonTaskDefinitionF7D7F4D9)
aws ecs describe-task-definition --task-definition "$(pid DaemonTaskDefinitionF7D7F4D9)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (DaemonTaskDefinitionExecutionRole7F0CF734)
aws iam get-role --role-name "$(pid DaemonTaskDefinitionExecutionRole7F0CF734)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (DaemonLogGroupBA9D5642)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/ec2-node-exporter" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ECS::Service (DaemonServiceFEA907A4)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-ec2" --services "${PRODUCT}-${ENV}-ec2-node-exporter" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
# Also created - listed in the table above:
#   11 VPC sub-resources (subnets, route tables, gateways, endpoints) - built by shared/network.py, listed one by one in modules/01_network/README.md
#   AWS::ECS::ClusterCapacityProviderAssociations Cluster3DA9CCBA - shown by its ECS cluster (describe-clusters --include ATTACHMENTS)
#   AWS::IAM::Policy InstanceRoleDefaultPolicy1531605C - shown by its IAM role
#   AWS::IAM::Policy WebTaskDefinitionExecutionRoleDefaultPolicy5D8A2D6F - shown by its IAM role
#   AWS::IAM::Policy DaemonTaskDefinitionExecutionRoleDefaultPolicyBBC182C0 - shown by its IAM role
```
<!-- END resource-commands -->

## Manage it with the AWS CLI

Capacity providers, run against floci while writing this (the ASG ARN is
looked up from the ASG this stack created):

```bash
ASG_ARN=$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names learning-ecs-dev-ec2-asg \
  --query 'AutoScalingGroups[0].AutoScalingGroupARN' --output text)

# create - managed scaling at 90% (keeps ~10% spare capacity for faster task starts)
aws ecs create-capacity-provider --name learning-ecs-dev-ec2-manual-cp \
  --auto-scaling-group-provider "autoScalingGroupArn=${ASG_ARN},managedScaling={status=ENABLED,targetCapacity=90,minimumScalingStepSize=1,maximumScalingStepSize=10,instanceWarmupPeriod=300},managedTerminationProtection=DISABLED,managedDraining=ENABLED"

# update
aws ecs update-capacity-provider --name learning-ecs-dev-ec2-manual-cp \
  --auto-scaling-group-provider "managedScaling={status=ENABLED,targetCapacity=80},managedTerminationProtection=DISABLED,managedDraining=ENABLED"

# attach to / detach from the cluster (the list you pass *replaces* the current one)
aws ecs put-cluster-capacity-providers --cluster learning-ecs-dev-ecs-ec2 \
  --capacity-providers learning-ecs-dev-ec2-asg-cp learning-ecs-dev-ec2-manual-cp \
  --default-capacity-provider-strategy capacityProvider=learning-ecs-dev-ec2-asg-cp,weight=1
aws ecs put-cluster-capacity-providers --cluster learning-ecs-dev-ecs-ec2 \
  --capacity-providers learning-ecs-dev-ec2-asg-cp \
  --default-capacity-provider-strategy capacityProvider=learning-ecs-dev-ec2-asg-cp,weight=1

# delete (must not be in use by the cluster or any service)
aws ecs delete-capacity-provider --capacity-provider learning-ecs-dev-ec2-manual-cp

# the ASG bounds (managed scaling only moves DesiredCapacity between them)
aws autoscaling update-auto-scaling-group --auto-scaling-group-name learning-ecs-dev-ec2-asg --min-size 0 --max-size 6
```

Services with placement rules and the DAEMON strategy - the same flags
`create-service` documents (reuse the network configuration of the web
service, as in [module 02](../02_fargate_service/README.md#manage-it-with-the-aws-cli-without-the-cdk)):

```bash
aws ecs create-service --cluster learning-ecs-dev-ecs-ec2 --service-name web-manual \
  --task-definition learning-ecs-dev-ec2-web --desired-count 2 \
  --capacity-provider-strategy capacityProvider=learning-ecs-dev-ec2-asg-cp,weight=1 \
  --network-configuration "$NETWORK_CONFIGURATION" \
  --placement-strategy type=spread,field=attribute:ecs.availability-zone type=binpack,field=memory
aws ecs update-service --cluster learning-ecs-dev-ecs-ec2 --service web-manual \
  --placement-strategy type=spread,field=instanceId          # change the strategy later

aws ecs create-service --cluster learning-ecs-dev-ecs-ec2 --service-name agent-manual \
  --task-definition learning-ecs-dev-ec2-node-exporter --scheduling-strategy DAEMON
```

Instance maintenance - real AWS only (floci has no container instances):

```bash
CI=$(aws ecs list-container-instances --cluster learning-ecs-dev-ecs-ec2 --query 'containerInstanceArns[0]' --output text)
# DRAINING: no new tasks are placed there; service tasks are replaced elsewhere first
aws ecs update-container-instances-state --cluster learning-ecs-dev-ecs-ec2 --container-instances "$CI" --status DRAINING
aws ecs describe-container-instances --cluster learning-ecs-dev-ecs-ec2 --container-instances "$CI" \
  --query 'containerInstances[0].[status,runningTasksCount]'
aws ecs update-container-instances-state --cluster learning-ecs-dev-ecs-ec2 --container-instances "$CI" --status ACTIVE

# Shell on the instance without SSH (the instance role has AmazonSSMManagedInstanceCore)
aws ssm start-session --target <ec2-instance-id>
# then: sudo cat /etc/ecs/ecs.config ; curl -s http://localhost:51678/v1/metadata (ECS agent introspection)
```

## Fargate or EC2?

| You need... | Fargate | EC2 (this module) |
|---|---|---|
| no servers to patch or scale | yes | no - AMI updates, ASG, agent |
| GPUs, specific instance families, very large tasks | limited | yes |
| per-host agents (DAEMON services) | no (use sidecars) | yes |
| fastest task start | depends on image pull | faster when the image is cached on the instance |
| best price at sustained high utilization | per-task pricing | per-instance pricing; binpack + Savings Plans/Spot |

Between the two sits **ECS Managed Instances** (AWS-managed EC2 instances
picked from your instance requirements); the CDK exposes it as
`aws_ecs.ManagedInstancesCapacityProvider` - see
[Amazon ECS Managed Instances](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/managed-instances-capacity-providers-concept.html).

## Metrics to watch

```bash
# How full the capacity provider is (drives managed scaling) - AWS/ECS/ManagedScaling.
# List the metric first to see the exact dimensions ECS publishes it with:
aws cloudwatch list-metrics --namespace AWS/ECS/ManagedScaling --metric-name CapacityProviderReservation
# ...then query it with those dimensions, e.g. with get-metric-statistics as below.

# Cluster-level reservation (AWS/ECS): how much CPU/memory the tasks reserve on the instances
aws cloudwatch get-metric-statistics --namespace AWS/ECS --metric-name MemoryReservation \
  --dimensions Name=ClusterName,Value=learning-ecs-dev-ecs-ec2 \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Average --output table
```

node-exporter serves host metrics on port 9100 of every instance
(`curl http://<instance-private-ip>:9100/metrics` from inside the VPC) -
[module 20](../20_prometheus_grafana/README.md) scrapes this kind of
endpoint with Prometheus. Full catalog: [`../../docs/METRICS.md`](../../docs/METRICS.md).

## Troubleshooting

| Symptom | Where to look | Typical cause / fix |
|---|---|---|
| Tasks stuck in `PROVISIONING`/`PENDING`, service event "unable to place a task" | `describe-services` -> `events` | no instance with enough free CPU/memory/ports: managed scaling is adding instances (check the ASG), or `CDK_EC2_MAX_CAPACITY` is reached |
| `list-container-instances` is empty | instance system log; `/var/log/ecs/ecs-agent.log` on the instance | the instance didn't join: wrong cluster name in `/etc/ecs/ecs.config`, missing instance role, no route to the ECS endpoint (NAT or VPC endpoints) |
| ASG doesn't scale in | `describe-policies`, `AWS/ECS/ManagedScaling` | managed termination protection keeps instances with tasks; the DAEMON task alone doesn't block scale-in |
| `cdk destroy` hangs/fails on the ASG | CloudFormation events | managed termination protection is on (`CDK_EC2_TERMINATION_PROTECTION=true`) - see [Notes](#notes-and-cautions) |
| Port conflict for a `host`/`bridge` task | service events | two tasks want the same host port on one instance - use `awsvpc`, or a `distinctInstance` constraint |

More: [`../../docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md).

## floci vs real AWS

| Behavior | floci 2.1.0 | Real AWS |
|---|---|---|
| Launch template, ASG, capacity provider, cluster association | created | created |
| EC2 instances in the ASG | **not launched** (`DesiredCapacity` 0) | launched, join the cluster |
| Container instances (`list-container-instances`) | none | one per instance |
| EC2-launch-type tasks | run directly as Docker containers on your machine | placed on the instances |
| Managed scaling / `CapacityProviderReservation` | not emulated; `autoScalingGroupProvider` isn't echoed by `describe-capacity-providers` | as described above |
| `SchedulingStrategy: DAEMON` from CloudFormation | reported as `REPLICA` with one task | one task per instance |
| Placement strategies/constraints | stored, not evaluated | evaluated |

## Clean up

```bash
uv run cdk destroy Ec2CapacityStack
uv run python scripts/floci_prune.py --apply   # floci only (REQUIREMENTS.md section 5.7)
```

## Notes and cautions

- **Cost**: see [Deploy to real AWS](#deploy-to-real-aws-optional).
- **Managed termination protection is off by default here** because of a
  [known CloudFormation issue](https://github.com/aws/containers-roadmap/issues/631)
  that keeps CloudFormation from deleting an ASG that has it on (the CDK's
  `aws_ecs` README documents this). In production you usually want it on
  (`CDK_EC2_TERMINATION_PROTECTION=true`), so scale-in never terminates an
  instance that still runs service tasks - and then delete the ASG by
  hand before `cdk destroy`.
- **Placement order matters below 100% target capacity**: the ECS docs
  require `binpack` to come *before* `spread` when `targetCapacity` is
  under 100%, otherwise the capacity provider can keep scaling out.
- **AMI updates replace instances**: `EcsOptimizedImage.amazon_linux2023()`
  resolves the *recommended* AMI at deploy time, so a newer AMI means new
  instances on the next deploy (managed draining moves the tasks first).
- **When managed scaling scales out from zero instances, ECS launches two**
  (per the cluster auto scaling docs) - keep `CDK_EC2_MAX_CAPACITY` >= 2.

## References

- [Amazon ECS capacity providers for EC2 workloads](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/asg-capacity-providers.html)
- [Automatically manage Amazon ECS capacity with cluster auto scaling](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cluster-auto-scaling.html)
- [Use strategies to define Amazon ECS task placement](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-placement-strategies.html) · [Task placement constraints](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-placement-constraints.html)
- [Amazon ECS services (REPLICA and DAEMON)](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_services.html)
- [Amazon ECS-optimized Linux AMIs](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-optimized_AMI.html)
- [Amazon ECS container instance IAM role](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/instance_IAM_role.html)
- [Draining Amazon ECS container instances](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/container-instance-draining.html)
- [Amazon ECS Managed Instances](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/managed-instances-capacity-providers-concept.html)
- [AWS CDK API Reference (Python) - `aws_cdk.aws_ecs.AsgCapacityProvider`](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/AsgCapacityProvider.html)
- [AWS CDK API Reference (Python) - `aws_cdk.aws_ecs.Ec2Service`](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ecs/Ec2Service.html)
- [AWS CDK API Reference (Python) - `aws_cdk.aws_autoscaling.AutoScalingGroup`](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_autoscaling/AutoScalingGroup.html)
- [Docker Hub - prom/node-exporter](https://hub.docker.com/r/prom/node-exporter)
