<!-- TOC -->

- [learning-ecs](#learning-ecs)
  - [What you get](#what-you-get)
  - [Quickstart (local, free)](#quickstart-local-free)
  - [The modules](#the-modules)
  - [How each module is organized](#how-each-module-is-organized)
  - [Documentation](#documentation)
  - [Contributing and license](#contributing-and-license)

<!-- TOC -->

# learning-ecs

A hands-on path to **running containers at scale on Amazon ECS**, from zero
to advanced, with the infrastructure written in the **AWS CDK (Python)** and
every module runnable **on your own machine for free** with
[floci](https://floci.io), a local AWS emulator. All the applications are
public images from Docker Hub.

You will deploy services on Fargate and EC2 capacity; expose them through
internal and internet-facing ALBs and NLBs, API Gateway and CloudFront;
connect them with Service Connect; back them with RDS (MySQL, PostgreSQL),
Aurora, ElastiCache, DocumentDB, SQS, SNS and S3; scale them, release them
with rolling, blue/green, canary and linear deployments; spread them over
several Availability Zones and two regions; and observe and troubleshoot
them with CloudWatch, Prometheus/Grafana and Datadog.

## What you get

- **21 modules**, each a CDK stack you can deploy, test and destroy.
- **Every AWS CLI command** to create, inspect, change and delete what a
  module teaches - run against floci while writing, and marked when they
  only work on real AWS.
- **Flexible configuration**: every knob is an environment variable with a
  default (`.env.example`, [`REQUIREMENTS.md` section 9](REQUIREMENTS.md#9-flexible-configuration)).
- **Metrics and troubleshooting** in every module, plus the cross-cutting
  [`docs/METRICS.md`](docs/METRICS.md) and [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).
- **Honest floci notes**: each module lists what floci emulates and what
  needs real AWS ([`REQUIREMENTS.md` section 10](REQUIREMENTS.md#10-floci-vs-real-aws)).
- Unit tests for every stack (`uv run pytest`, ~99% coverage).

## Quickstart (local, free)

Prerequisites: Docker, uv, Node.js + the AWS CDK CLI, the AWS CLI, jq - see
[`REQUIREMENTS.md`](REQUIREMENTS.md) (`make check` tells you what's missing).

```bash
git clone <this-repository-url> && cd learning-ecs
uv sync
docker compose up -d floci
cp .env.example .env && set -a && source .env && set +a
uv run cdk bootstrap
uv run cdk list                      # 23 stacks

# module 04: two services behind an internet-facing and an internal ALB
uv run cdk deploy AlbStack --require-approval never --method=direct
curl -s localhost:8081/ | grep Name  # Name: web
uv run cdk destroy AlbStack
uv run python scripts/floci_prune.py --apply
```

Then start the path at [`modules/01_network`](modules/01_network/README.md).

## The modules

| Phase | Modules |
|---|---|
| 1. Foundations | [01 Network](modules/01_network/README.md) · [02 Fargate service](modules/02_fargate_service/README.md) · [03 EC2 capacity](modules/03_ec2_capacity/README.md) |
| 2. Exposing services | [04 ALB](modules/04_alb/README.md) · [05 NLB](modules/05_nlb/README.md) · [06 API Gateway](modules/06_api_gateway/README.md) · [07 CloudFront](modules/07_cloudfront/README.md) · [08 Service Connect](modules/08_service_connect/README.md) |
| 3. Data and messaging | [09 RDS MySQL](modules/09_rds_mysql/README.md) · [10 RDS PostgreSQL](modules/10_rds_postgresql/README.md) · [11 Aurora](modules/11_aurora/README.md) · [12 ElastiCache](modules/12_elasticache/README.md) · [13 DocumentDB](modules/13_documentdb/README.md) · [14 SQS + SNS](modules/14_sqs_sns/README.md) · [15 S3](modules/15_s3/README.md) |
| 4. Scaling and releasing | [16 Service Auto Scaling](modules/16_autoscaling/README.md) · [17 Deployments](modules/17_deployments/README.md) |
| 5. Resilience across regions | [18 Multi-region](modules/18_multi_region/README.md) |
| 6. Observability | [19 CloudWatch](modules/19_cloudwatch/README.md) · [20 Prometheus and Grafana](modules/20_prometheus_grafana/README.md) · [21 Datadog](modules/21_datadog/README.md) |

What each module covers, its stack id and what runs on floci:
[`docs/LEARNING-PATH.md`](docs/LEARNING-PATH.md).

## How each module is organized

`modules/NN_topic/` holds `stack.py` (the CDK stack) and a `README.md`
with: Overview · What you will learn · Architecture · AWS services and CDK
constructs used · Configuration · Tests · Deploy with floci · Deploy to
real AWS · Verify (including a generated "List every resource with the AWS
CLI") · Manage it with the AWS CLI · Metrics to watch · Troubleshooting ·
floci vs real AWS · Clean up · Notes and cautions · References (official
sources).

## Documentation

| File | What's in it |
|---|---|
| [`REQUIREMENTS.md`](REQUIREMENTS.md) | install, floci, bootstrap, ports, tagging and naming, configuration, floci vs real AWS |
| [`docs/LEARNING-PATH.md`](docs/LEARNING-PATH.md) | the 21 modules in order |
| [`docs/METRICS.md`](docs/METRICS.md) | every metric used, in CloudWatch, Prometheus and Datadog |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | from symptom to cause and fix |
| [`docs/TESTING.md`](docs/TESTING.md) | the unit tests |
| [`CLAUDE.md`](CLAUDE.md) | conventions and guardrails for maintainers (and AI assistants) |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) · [`CHANGELOG.md`](CHANGELOG.md) | contributing, changes |

## Contributing and license

Contributions are welcome - see [`CONTRIBUTING.md`](CONTRIBUTING.md).
Licensed under the GNU General Public License v3.0 - see [`LICENSE`](LICENSE).
Deploying to a real AWS account costs money; each module's "Deploy to real
AWS" section says what it bills for. Destroy what you deploy.
