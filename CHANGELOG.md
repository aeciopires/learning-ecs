<!-- TOC -->

- [Changelog](#changelog)
  - [\[0.1.0\] - 2026-10-02](#010---2026-10-02)
    - [Added](#added)

<!-- TOC -->

# Changelog

All notable changes to this project are documented in this file. The format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-10-02

### Added

- Repository scaffold following the conventions of
  [`learning-cdk-python`](https://github.com/aeciopires/learning-cdk-python):
  `pyproject.toml` (uv, `aws-cdk-lib==2.272.0`), `.python-version` (3.14),
  `mise.toml`, `cdk.json`, `app.py` (module discovery, plus the optional
  `build_stacks()` hook for modules with several stacks), `.env.example`,
  `Makefile`, `docker-compose.yml` (floci, tested with floci 2.1.0).
- `shared/`: `config.py` (validated `CDK_*` variables), `tagging.py`,
  `naming.py` (`resource_name()`, `bounded_name()`), `network.py`,
  `ecs.py` (cluster, Fargate service, logs, ports, Spot, architecture,
  Container Insights, ECS Exec), `database.py`, `apps.py`.
- `scripts/`: `check-deps.sh`, `floci_prune.py`, `resource_commands.py`
  (generated "List every resource with the AWS CLI" sections, with handlers
  for the ECS, load balancing, API Gateway, CloudFront, Cloud Map,
  Application Auto Scaling, Scheduler, CloudWatch Logs, AMP and DocumentDB
  types this path uses).
- 21 modules, each with a CDK stack, unit tests and a README (explanations,
  configuration, floci and real-AWS deploys, verification, AWS CLI
  management commands, metrics, troubleshooting, floci vs real AWS):
  01 network, 02 Fargate service, 03 EC2 capacity providers, 04 ALB,
  05 NLB, 06 API Gateway, 07 CloudFront, 08 Service Connect, 09 RDS MySQL,
  10 RDS PostgreSQL, 11 Aurora, 12 ElastiCache, 13 DocumentDB, 14 SQS + SNS,
  15 S3 + scheduled tasks, 16 Service Auto Scaling, 17 deployments
  (rolling, circuit breaker, alarms, blue/green, canary, linear),
  18 multi-region, 19 CloudWatch, 20 Prometheus and Grafana, 21 Datadog.
- `REQUIREMENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `README.md`,
  `docs/LEARNING-PATH.md`, `docs/TESTING.md`, `docs/METRICS.md`,
  `docs/TROUBLESHOOTING.md`.
