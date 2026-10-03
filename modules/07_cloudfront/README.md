<!-- TOC -->

- [Module 07 - CloudFront (CDN in front of an ECS service)](#module-07---cloudfront-cdn-in-front-of-an-ecs-service)
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

# Module 07 - CloudFront (CDN in front of an ECS service)

## Overview

**Amazon CloudFront** puts AWS's edge network in front of your service:
TLS termination close to the user, caching of what can be cached, DDoS
absorption, AWS WAF, geo restrictions - and your load balancer only sees
the requests CloudFront couldn't answer from its cache.

The catch: if the load balancer is internet-facing, users can also go
*around* CloudFront. This module shows both ways AWS documents to close
that door, chosen with `CDK_CLOUDFRONT_ORIGIN`:

- **`public_alb`** (default) - an internet-facing ALB; CloudFront adds a
  **secret header** (`X-Origin-Verify`, value generated in Secrets
  Manager) to every origin request; the ALB **forwards only requests with
  that header** and answers `403` to everything else. Optionally
  (`CDK_CLOUDFRONT_PREFIX_LIST_ID`), the ALB's security group admits only
  CloudFront's origin-facing addresses (the managed prefix list
  `com.amazonaws.global.cloudfront.origin-facing`).
- **`vpc_origin`** - an **internal** ALB in private subnets reached through
  a **CloudFront VPC origin**: there is no public entry point at all.

## What you will learn

- Distributions, origins and cache behaviors; managed cache and origin
  request policies (`CachingDisabled` + `AllViewerExceptHostHeader` for the
  dynamic default, `CachingOptimized` for `/static/*`).
- Restricting an ALB origin to CloudFront: custom header + listener rule +
  default `403`, the managed prefix list, and VPC origins.
- Keeping a secret out of the template with a Secrets Manager dynamic
  reference.
- Invalidations, enabling/disabling and deleting a distribution with the
  CLI (the `ETag`/`--if-match` dance).
- CloudFront metrics (published in `us-east-1`, dimension `Region=Global`).

## Architecture

```
 CDK_CLOUDFRONT_ORIGIN=public_alb (default)
   viewer --HTTPS--> CloudFront distribution  (HTTP -> HTTPS redirect)
                       default (*)       : CachingDisabled, AllViewerExceptHostHeader --+
                       /static/*         : CachingOptimized, compressed ---------------+
                                                                                       | + header X-Origin-Verify: <secret>
                                                                                       v
                     internet-facing ALB  [SG: 0.0.0.0/0, or only the CloudFront prefix list]
                       rule 10: header X-Origin-Verify == <secret>  -> forward -> whoami tasks
                       default                                       -> fixed 403 "forbidden"

 CDK_CLOUDFRONT_ORIGIN=vpc_origin
   viewer --HTTPS--> CloudFront -- VPC origin (service-managed ENIs in your private subnets) -->
                     internal ALB [SG: CloudFront prefix list] -> whoami tasks
```

## AWS services and CDK constructs used

| AWS service | CDK construct (Python) | Level |
|---|---|---|
| Amazon CloudFront | `aws_cdk.aws_cloudfront.Distribution`, `BehaviorOptions`, `CachePolicy`, `OriginRequestPolicy` | L2 |
| Amazon CloudFront | `aws_cdk.aws_cloudfront_origins.LoadBalancerV2Origin`, `HttpOrigin`, `VpcOrigin.with_application_load_balancer` | L2 |
| Elastic Load Balancing | `ApplicationLoadBalancer`, `ListenerCondition.http_header`, `ListenerAction.fixed_response` | L2 |
| AWS Secrets Manager | `aws_cdk.aws_secretsmanager.Secret` + `SecretValue.secrets_manager(<name>)` | L2 |
| Amazon VPC | `aws_cdk.aws_ec2.Peer.prefix_list` | L2 (helper) |

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CDK_CLOUDFRONT_ORIGIN` | `public_alb` | `public_alb` or `vpc_origin` |
| `CDK_CLOUDFRONT_PREFIX_LIST_ID` | unset | id of `com.amazonaws.global.cloudfront.origin-facing` in your region; restricts the ALB's security group (required for `vpc_origin`) |
| `CDK_CLOUDFRONT_PRICE_CLASS` | `100` | `100`, `200` or `all` - which edge locations serve the distribution |
| `CDK_CLOUDFRONT_ORIGIN_DOMAIN` | the ALB's DNS name (`.env.example`: `localhost`) | origin host override - floci only (see [floci vs real AWS](#floci-vs-real-aws)) |
| `CDK_CLOUDFRONT_IMAGE` / `CDK_CLOUDFRONT_DESIRED_COUNT` | `traefik/whoami:v1.12.0` / `2` | the service |
| `CDK_PORT_CLOUDFRONT_ALB` | `80` (`.env.example`: `8086`) | the ALB listener port |

Find the prefix list id (real AWS, any region):

```bash
aws ec2 describe-managed-prefix-lists \
  --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
  --query 'PrefixLists[0].PrefixListId' --output text
```

## Prerequisites

[Module 04](../04_alb/README.md). floci running and `.env` loaded;
`docker-compose.yml` already sets `FLOCI_SERVICES_CLOUDFRONT_ALLOWED_PRIVATE_ORIGIN_HOSTS: localhost`
(re-create the container with `docker compose up -d floci` if you started
it before pulling this change).

## Tests

[`../../tests/unit/test_07_cloudfront.py`](../../tests/unit/test_07_cloudfront.py)
checks the public ALB origin and its secret header (a dynamic reference,
never the value), the header-matching forward rule and the default `403`,
the two cache behaviors and price class, the prefix-list restriction (and
that no `0.0.0.0/0` rule remains), the VPC origin variant and its required
prefix list, the floci origin override, invalid choices, and the mandatory
tags:

```bash
uv run pytest tests/unit/test_07_cloudfront.py -v
```

## Deploy with floci (local, free)

```bash
uv run cdk bootstrap
uv run cdk synth CloudFrontStack
uv run cdk diff CloudFrontStack
uv run cdk deploy CloudFrontStack --require-approval never --method=direct
```

## Deploy to real AWS (optional)

CloudFront bills per request and per GB delivered; the ALB, NAT Gateway
and tasks bill hourly - see [Amazon CloudFront pricing](https://aws.amazon.com/cloudfront/pricing/).
A new distribution takes several minutes to reach `Deployed`.

```bash
unset AWS_ENDPOINT_URL CDK_CLOUDFRONT_ORIGIN_DOMAIN CDK_PORT_CLOUDFRONT_ALB   # REQUIREMENTS.md section 9.2
export CDK_CLOUDFRONT_PREFIX_LIST_ID=$(aws ec2 describe-managed-prefix-lists \
  --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
  --query 'PrefixLists[0].PrefixListId' --output text --profile <your-aws-cli-profile>)
uv run cdk bootstrap --profile <your-aws-cli-profile>
uv run cdk diff CloudFrontStack --profile <your-aws-cli-profile>
uv run cdk deploy CloudFrontStack --profile <your-aws-cli-profile>
# VPC origin instead: CDK_CLOUDFRONT_ORIGIN=vpc_origin uv run cdk deploy CloudFrontStack --profile ...
```

## Verify

```bash
DIST=$(aws cloudformation describe-stacks --stack-name CloudFrontStack \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" --output text)
ALB=$(aws cloudformation describe-stacks --stack-name CloudFrontStack \
  --query "Stacks[0].Outputs[?OutputKey=='AlbDns'].OutputValue" --output text)

# Real AWS
curl -s "https://$(aws cloudfront get-distribution --id "$DIST" --query Distribution.DomainName --output text)/hello" | grep -E '^(Name|GET)'
curl -s -o /dev/null -w 'direct to the ALB: %{http_code}\n' "http://$ALB/"          # 403 (or a timeout with the prefix list)

# floci: CloudFront answers on floci's endpoint for the distribution's Host name
curl -s -H "Host: $DIST.cloudfront.net" http://localhost:4566/hello | grep -E '^(Name|GET|X-Origin-Verify)'
curl -s -w ' <- direct to the ALB: %{http_code}\n' http://localhost:${CDK_PORT_CLOUDFRONT_ALB:-8086}/
```

`whoami` echoes the request headers - so the response above *shows the
secret header value*. That's useful to see how the pattern works, and
exactly why a real application must never echo request headers back
(rotate the secret after this exercise - see [Notes](#notes-and-cautions)).

The ALB's rules and the distribution's origin:

```bash
LB=$(aws elbv2 describe-load-balancers --names learning-ecs-dev-cf-alb --query 'LoadBalancers[0].LoadBalancerArn' --output text)
aws elbv2 describe-rules --listener-arn "$(aws elbv2 describe-listeners --load-balancer-arn "$LB" --query 'Listeners[0].ListenerArn' --output text)" \
  --query 'Rules[].[Priority,Conditions[0].HttpHeaderConfig.HttpHeaderName,Actions[0].Type]' --output table
aws cloudfront get-distribution --id "$DIST" \
  --query 'Distribution.DistributionConfig.Origins.Items[0].[DomainName,CustomHeaders.Items[0].HeaderName]'
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
`make cdk-resources STACK=CloudFrontStack` runs the same commands for you.

```bash
# Match these to your deployment: CDK_PRODUCT and CDK_ENVIRONMENT in .env, and
# the region you deployed to (floci: the one in .env).
PRODUCT=learning-ecs ENV=dev REGION=us-east-1
STACK=CloudFrontStack
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
aws ecs describe-clusters --clusters "${PRODUCT}-${ENV}-ecs-cloudfront" --query "clusters[].[clusterName,status]" --output table --region "$REGION"
# AWS::IAM::Role (WebTaskDefinitionTaskRole2EE1C0E7)
aws iam get-role --role-name "$(pid WebTaskDefinitionTaskRole2EE1C0E7)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::ECS::TaskDefinition (WebTaskDefinition8DF7C630)
aws ecs describe-task-definition --task-definition "$(pid WebTaskDefinition8DF7C630)" --query "taskDefinition.[family,revision,status]" --output table --region "$REGION"
# AWS::IAM::Role (WebTaskDefinitionExecutionRole225B46C9)
aws iam get-role --role-name "$(pid WebTaskDefinitionExecutionRole225B46C9)" --query "Role.[RoleName,Arn]" --output table --region "$REGION"
# AWS::Logs::LogGroup (WebLogGroup68B8CF3C)
aws logs describe-log-groups --log-group-name-prefix "/ecs/${PRODUCT}/${ENV}/cloudfront-web" --query "logGroups[].[logGroupName,retentionInDays]" --output table --region "$REGION"
# AWS::ECS::Service (WebService7F8A1763)
aws ecs describe-services --cluster "${PRODUCT}-${ENV}-ecs-cloudfront" --services "${PRODUCT}-${ENV}-cloudfront-web" --query "services[].[serviceName,status,desiredCount]" --output table --region "$REGION"
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
# AWS::SecretsManager::Secret (OriginVerifySecret1DED2FB3)
aws secretsmanager describe-secret --secret-id "${PRODUCT}-${ENV}-cloudfront-origin-verify" --query "[Name,ARN]" --output table --region "$REGION"
# AWS::CloudFront::Distribution (Distribution830FAC52)
aws cloudfront get-distribution --id "$(pid Distribution830FAC52)" --query "Distribution.[Id,DomainName,Status]" --output table --region "$REGION"
# Also created - listed in the table above:
#   11 VPC sub-resources (subnets, route tables, gateways, endpoints) - built by shared/network.py, listed one by one in modules/01_network/README.md
#   AWS::ECS::ClusterCapacityProviderAssociations Cluster3DA9CCBA - shown by its ECS cluster (describe-clusters --include ATTACHMENTS)
#   AWS::IAM::Policy WebTaskDefinitionTaskRoleDefaultPolicyD1A8300E - shown by its IAM role
#   AWS::IAM::Policy WebTaskDefinitionExecutionRoleDefaultPolicy5D8A2D6F - shown by its IAM role
#   AWS::EC2::SecurityGroupIngress WebServiceSecurityGroupfromCloudFrontStackAlbSecurityGroupECDEE4EE80BE3A45C5 - shown by its security group
#   AWS::EC2::SecurityGroupEgress AlbSecurityGrouptoCloudFrontStackWebServiceSecurityGroup6A9D4C8D804B582138 - shown by its security group
#   AWS::ElasticLoadBalancingV2::ListenerRule AlbHttpWebRuleE62B4752 - shown by its listener (elbv2 describe-rules)
```
<!-- END resource-commands -->

## Manage it with the AWS CLI

A distribution, created, invalidated, disabled and deleted by hand - run
against floci while writing this module. Every update/delete needs the
current `ETag` (`--if-match`), and a distribution must be **disabled**
(and deployed) before it can be deleted. `4135ea2d-...` and `b689b0a8-...`
are the ids of the managed `CachingDisabled` cache policy and
`AllViewerExceptHostHeader` origin request policy (the same ids the CDK
synthesizes):

```bash
cat > dist.json <<'JSON'
{
  "CallerReference": "learning-ecs-manual-1",
  "Comment": "learning-ecs-dev-cloudfront-manual",
  "Enabled": true,
  "PriceClass": "PriceClass_100",
  "Origins": {
    "Quantity": 1,
    "Items": [{
      "Id": "alb",
      "DomainName": "<alb-dns-name>",
      "CustomOriginConfig": {
        "HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only",
        "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]}
      },
      "CustomHeaders": {"Quantity": 1, "Items": [{"HeaderName": "X-Origin-Verify", "HeaderValue": "<secret>"}]}
    }]
  },
  "DefaultCacheBehavior": {
    "TargetOriginId": "alb",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {"Quantity": 7, "Items": ["GET","HEAD","OPTIONS","PUT","PATCH","POST","DELETE"],
                       "CachedMethods": {"Quantity": 2, "Items": ["GET","HEAD"]}},
    "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
    "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac",
    "Compress": true
  }
}
JSON
# create
DIST=$(aws cloudfront create-distribution --distribution-config file://dist.json --query Distribution.Id --output text)
aws cloudfront wait distribution-deployed --id "$DIST"

# invalidate cached objects (e.g. after a deploy that changed /static/*)
INV=$(aws cloudfront create-invalidation --distribution-id "$DIST" --paths '/static/*' --query Invalidation.Id --output text)
aws cloudfront get-invalidation --distribution-id "$DIST" --id "$INV" --query Invalidation.Status

# update (here: disable) - get the config + ETag, change it, send it back
ETAG=$(aws cloudfront get-distribution-config --id "$DIST" --query ETag --output text)
aws cloudfront get-distribution-config --id "$DIST" --query DistributionConfig > current.json
jq '.Enabled=false' current.json > disabled.json
aws cloudfront update-distribution --id "$DIST" --if-match "$ETAG" --distribution-config file://disabled.json
aws cloudfront wait distribution-deployed --id "$DIST"

# delete
ETAG=$(aws cloudfront get-distribution-config --id "$DIST" --query ETag --output text)
aws cloudfront delete-distribution --id "$DIST" --if-match "$ETAG"
```

Rotating the origin secret. Note first what CloudFormation does *not* do:
"Updating only the secret value in Secrets Manager doesn't automatically
cause CloudFormation to retrieve the new value" - it resolves the dynamic
reference only when it creates or updates the resource that contains it
([Get a secret or secret value from Secrets Manager](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references-secretsmanager.html)).
So a plain `cdk deploy` after `put-secret-value` changes nothing. AWS's
zero-downtime procedure (add the new header value, then remove the old
one) by hand:

```bash
NEW=$(openssl rand -hex 16)
LISTENER=$(aws elbv2 describe-listeners --load-balancer-arn "$LB" --query 'Listeners[0].ListenerArn' --output text)
TG=$(aws elbv2 describe-rules --listener-arn "$LISTENER" \
  --query "Rules[?Priority=='10'].Actions[0].TargetGroupArn" --output text)

# 1. the ALB accepts the new value too
NEW_RULE=$(aws elbv2 create-rule --listener-arn "$LISTENER" --priority 11 \
  --conditions "Field=http-header,HttpHeaderConfig={HttpHeaderName=X-Origin-Verify,Values=[$NEW]}" \
  --actions Type=forward,TargetGroupArn="$TG" --query 'Rules[0].RuleArn' --output text)
# 2. CloudFront sends the new value
ETAG=$(aws cloudfront get-distribution-config --id "$DIST" --query ETag --output text)
aws cloudfront get-distribution-config --id "$DIST" --query DistributionConfig \
  | jq --arg v "$NEW" '.Origins.Items[0].CustomHeaders.Items[0].HeaderValue=$v' > rotated.json
aws cloudfront update-distribution --id "$DIST" --if-match "$ETAG" --distribution-config file://rotated.json
aws cloudfront wait distribution-deployed --id "$DIST"
# 3. store it, so the next stack update that touches these resources resolves the same value
aws secretsmanager put-secret-value --secret-id learning-ecs-dev-cloudfront-origin-verify --secret-string "$NEW"
```

Then remove the old value from the priority-10 rule (`aws elbv2 modify-rule
--rule-arn <rule-10-arn> --conditions ...` with `$NEW`, then
`aws elbv2 delete-rule --rule-arn "$NEW_RULE"`). These manual changes are
*drift* from what CloudFormation last applied; the next deploy that updates
these resources re-resolves the secret (now `$NEW`), so they converge.

## Metrics to watch

`AWS/CloudFront`, read from **`us-east-1`**, dimensions
`DistributionId=<id>` and `Region=Global`:

| Metric | Statistic | Notes |
|---|---|---|
| `Requests` | Sum | all viewer requests |
| `BytesDownloaded` / `BytesUploaded` | Sum | |
| `4xxErrorRate` / `5xxErrorRate` / `TotalErrorRate` | Average (percent) | `5xxErrorRate` rising: the origin (ALB/tasks) is failing |
| `CacheHitRate` | Average | additional metric - must be turned on (extra cost) |
| `OriginLatency` | percentile (`--extended-statistics p90`) | additional metric; time to first byte from the origin |
| `401ErrorRate`, `403ErrorRate`, `404ErrorRate`, `502ErrorRate`, `503ErrorRate`, `504ErrorRate` | Average | additional metrics |

```bash
aws cloudwatch get-metric-statistics --region us-east-1 --namespace AWS/CloudFront --metric-name 5xxErrorRate \
  --dimensions Name=DistributionId,Value="$DIST" Name=Region,Value=Global \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 --statistics Average --output table
```

## Troubleshooting

| Symptom | Where to look | Typical cause / fix |
|---|---|---|
| CloudFront returns `403` from the origin | ALB rules | the header value CloudFront sends doesn't match the rule (secret rotated without redeploying) |
| `502` from CloudFront | origin protocol/port | CloudFront couldn't connect or complete TLS to the origin - check `OriginProtocolPolicy`, the ALB listener port, the ALB security group (prefix list) |
| `504` from CloudFront | `OriginLatency`, ALB `TargetResponseTime` | origin slower than the origin response timeout |
| Stale content after a deploy | cache behavior | invalidate the paths (`create-invalidation`) or version your static file names |
| Personalized content served to the wrong user | cache policy | a cached behavior doesn't vary on the cookie/header that personalizes - keep dynamic paths on `CachingDisabled` |
| VPC origin stays not `Deployed` / timeouts | VPC origin prerequisites | the VPC needs an internet gateway, a private subnet with a free IP, and the origin's security group must admit the prefix list or `CloudFront-VPCOrigins-Service-SG` |

More: [`../../docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md) and
[Troubleshooting error responses from your origin](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/troubleshooting-response-errors.html).

## floci vs real AWS

| Behavior | floci 2.1.0 | Real AWS |
|---|---|---|
| Distribution, origins, behaviors, custom headers (with the secret resolved), invalidations | created and working; viewer requests are proxied to the origin | same |
| Viewer URL | `http://localhost:4566/` with `Host: <id>.cloudfront.net` | `https://<id>.cloudfront.net/` |
| Origin `*.elb.floci` | not resolvable by floci's own CloudFront - `CDK_CLOUDFRONT_ORIGIN_DOMAIN=localhost` | the ALB's DNS name |
| Private origin addresses | refused unless allow-listed (`FLOCI_SERVICES_CLOUDFRONT_ALLOWED_PRIVATE_ORIGIN_HOSTS`) | n/a |
| Caching | no persistent edge cache; cache policies aren't evaluated | cached per policy |
| VPC origins | not supported | supported (in the regions AWS lists) |
| Deployment time | immediate (`Deployed`) | minutes |
| Metrics | not produced | as above |

## Clean up

```bash
uv run cdk destroy CloudFrontStack
uv run python scripts/floci_prune.py --apply   # floci only
```

On real AWS, deleting a distribution takes several minutes (it has to be
disabled and deployed first; CloudFormation does that as part of the delete).

## Notes and cautions

- **Cost**: see [Deploy to real AWS](#deploy-to-real-aws-optional).
- **Use HTTPS to the origin in production.** This module uses HTTP between
  CloudFront and the ALB because an HTTPS origin needs a domain name and an
  ACM certificate on the ALB; AWS recommends HTTPS so the secret header
  can't be read in transit (and periodic rotation).
- **Certificates for a custom domain on the distribution must be in ACM in
  `us-east-1`**, whatever region the ALB is in.
- **The header is only as secret as your logs and apps keep it** - don't
  log or echo it (this module's `whoami` does, on purpose).

## References

- [Restrict access to Application Load Balancers](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/restrict-access-to-load-balancer.html)
- [Restrict access with VPC origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html)
- [Use managed cache policies](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html) · [Use managed origin request policies](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-origin-request-policies.html)
- [Invalidate files to remove content](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/Invalidation.html)
- [Types of metrics for CloudFront](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/programming-cloudwatch-metrics.html)
- [Amazon CloudFront pricing](https://aws.amazon.com/cloudfront/pricing/)
- [AWS CDK API Reference (Python) - `aws_cdk.aws_cloudfront_origins`](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_cloudfront_origins/README.html) · [`aws_cdk.aws_cloudfront.Distribution`](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_cloudfront/Distribution.html)
- [AWS CLI Command Reference - `cloudfront`](https://docs.aws.amazon.com/cli/latest/reference/cloudfront/)
