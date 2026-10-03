<!-- TOC -->

- [CLAUDE.md](#claudemd)
  - [1. About this repository](#1-about-this-repository)
  - [2. Directory and file structure](#2-directory-and-file-structure)
  - [3. The module contract](#3-the-module-contract)
  - [4. Do not invent things: the core guardrail](#4-do-not-invent-things-the-core-guardrail)
  - [5. Tagging and naming (non-negotiable)](#5-tagging-and-naming-non-negotiable)
  - [6. Account/region flexibility and configuration (non-negotiable)](#6-accountregion-flexibility-and-configuration-non-negotiable)
  - [7. Applications, images and floci](#7-applications-images-and-floci)
  - [8. Markdown formatting conventions](#8-markdown-formatting-conventions)
  - [9. Python/CDK conventions](#9-pythoncdk-conventions)
  - [10. Workflow when adding or changing a module](#10-workflow-when-adding-or-changing-a-module)
  - [11. What not to do](#11-what-not-to-do)
  - [12. Environment notes](#12-environment-notes)

<!-- TOC -->

# CLAUDE.md

Guide for anyone - human or an AI coding assistant like Claude Code -
maintaining or extending this repository. Read this before adding or
editing a module.

## 1. About this repository

`learning-ecs` is a **public, beginner-oriented** learning path for running
containers at scale on [Amazon ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/Welcome.html),
from a first Fargate service to multi-region deployments and observability
with CloudWatch, Prometheus/Grafana and Datadog. Infrastructure is written
with the [AWS CDK v2](https://docs.aws.amazon.com/cdk/v2/guide/home.html) in
Python ([uv](https://docs.astral.sh/uv/) manages dependencies). It is 21
modules (`modules/NN_topic/`), each a deployable CDK stack plus a README
with explanations, every AWS CLI command to create, inspect, change and
delete what it teaches, metrics, troubleshooting, and a "floci vs real AWS"
table. Every module is deployed first against [floci](https://floci.io), a
local AWS emulator ([`docker-compose.yml`](docker-compose.yml)), so the
whole path works without an AWS account or any cost; deploying to real AWS
is optional.

It is **not** tied to any company: names, tags and examples use the generic
`learning-ecs` product and placeholder team/environment values. All
documentation is **en-US**.

## 2. Directory and file structure

```
.
├── README.md               # landing page: what this is, quickstart, module list
├── LICENSE                 # do not modify without being asked
├── REQUIREMENTS.md         # prerequisites, floci, ports, tagging/naming, configuration, floci vs real AWS
├── CONTRIBUTING.md         # how to propose a change or a new module
├── CHANGELOG.md            # Keep a Changelog style
├── CLAUDE.md               # this file
├── pyproject.toml          # aws-cdk-lib, constructs, boto3, dev tools (pytest, ruff, mypy, coverage)
├── .python-version         # Python 3.14
├── mise.toml               # Python, Node.js 26, npm 11, AWS CLI v2
├── cdk.json                # "app": "uv run python app.py"
├── app.py                  # discovers every modules/*/stack.py - see section 3
├── docker-compose.yml      # floci (learning-ecs-floci), ports 4566, 8080-8099, 7001-7099, 6379-6399
├── .env.example            # every shared CDK_*/AWS_* variable, the floci ports and floci-only overrides
├── Makefile                # optional shortcuts - REQUIREMENTS.md section 5.4
├── scripts/
│   ├── check-deps.sh          # `make check`
│   ├── floci_prune.py         # floci-only: deletes the VPCs `cdk destroy` leaves - REQUIREMENTS.md 5.7
│   └── resource_commands.py   # generates each README's "List every resource" section; `make cdk-resources` - 5.8
├── shared/
│   ├── config.py     # AppConfig, env_str/env_int/env_bool, get_environment()
│   ├── tagging.py    # the 7 tags
│   ├── naming.py     # resource_name(), bounded_name()
│   ├── network.py    # build_vpc(), task_subnets()
│   ├── ecs.py        # build_cluster(), fargate_service(), log_driver(), listener_port(), ...
│   ├── database.py   # generated credentials, secrets referenced by name
│   └── apps.py       # Docker Hub images and commands shared by several modules
├── docs/
│   ├── LEARNING-PATH.md     # the 21 modules in 6 phases
│   ├── TESTING.md           # how the unit tests work and how to write one
│   ├── METRICS.md           # every metric the path uses, by namespace/tool
│   └── TROUBLESHOOTING.md   # symptoms -> causes -> fixes, with CloudWatch, Prometheus/Grafana and Datadog
├── modules/
│   └── NN_topic/            # __init__.py, stack.py, README.md - see section 3
└── tests/
    ├── conftest.py           # the `config` fixture; removes CDK_* variables before each test
    ├── _helpers.py           # stack_class(), mandatory_tag_pairs()
    └── unit/
        ├── test_NN_topic.py  # one per module - mandatory
        ├── test_app.py       # app.py discovery and build_stacks()
        ├── test_floci_prune.py / test_resource_commands.py
        └── test_shared_*.py
```

**A new module takes the next free number** and is added to
[`docs/LEARNING-PATH.md`](docs/LEARNING-PATH.md); existing modules are never
renumbered. `modules/02_fargate_service/` is the reference for a service
module (its stack spells out what `shared/ecs.fargate_service()` hides) and
`modules/01_network/` for the network - read them before writing a module.

## 3. The module contract

`app.py` discovers modules with `pkgutil.iter_modules` over `modules/`, so
adding a module never requires editing `app.py`. Every
`modules/NN_topic/stack.py` provides:

1. A `Stack` subclass with exactly
   `(self, scope: Construct, construct_id: str, *, config: AppConfig, **kwargs) -> None`.
2. `STACK_ID: str` (PascalCase, e.g. `"AlbStack"`) and `STACK_CLASS`.
3. Optionally `build_stacks(app, *, config, env) -> list[Stack]`, when the
   module needs several stacks (module 18: one per region). `app.py` then
   calls it instead of instantiating `STACK_CLASS`.
4. `apply_standard_tags(self, tags=config.to_standard_tags())` right after
   `super().__init__(...)`, and `apply_name_tag(<construct>, <name>)` once
   per significant resource (section 5).
5. A module docstring listing the official docs used, and outputs
   (`CfnOutput`) for everything the README's commands need (URLs, cluster
   and service names, task definition ARNs - never a bare family on floci,
   see `REQUIREMENTS.md` section 5.6).
6. A `README.md` with these sections, in this order (a module may add an
   explainer section such as "ALB or NLB?" or "How the pieces fit" after
   "Architecture" or "Configuration"): Overview, What you will learn,
   Architecture, AWS services and CDK constructs used, Configuration,
   Prerequisites, Tests, Deploy with floci (local, free), Deploy to real
   AWS (optional), Verify (ending with "List every resource with the AWS
   CLI"), Manage it with the AWS CLI, Metrics to watch, Troubleshooting,
   floci vs real AWS, Clean up, Notes and cautions, References.
   - The floci deploy runs `uv run cdk bootstrap`, `cdk synth`, `cdk diff`
     and `cdk deploy <StackId> --require-approval never --method=direct`;
     the real-AWS deploy unsets `AWS_ENDPOINT_URL`, keeps the default
     method and states the cost.
   - "Clean up" runs `cdk destroy` and then
     `uv run python scripts/floci_prune.py --apply` (floci never deletes VPCs).
   - The "List every resource with the AWS CLI" block is **generated**,
     never hand-written: synthesize with `.env.example` loaded and run
     `uv run python scripts/resource_commands.py --markdown <StackId> --template cdk.out/<StackId>.template.json --readme modules/NN_topic/README.md`.
     A resource type without a handler shows "shown by the table above" -
     add a handler (and its floci status) to the script instead;
     `tests/unit/test_resource_commands.py` fails on any unhandled type.
   - "Manage it with the AWS CLI" commands were **run against floci** while
     writing; commands floci doesn't support are marked "real AWS only".
   - "Configuration" lists every `CDK_<MODULE>_*` variable, its default and
     effect.
7. A `tests/unit/test_NN_topic.py` (same number) that synthesizes the stack
   with the `config` fixture and asserts on the template with
   `aws_cdk.assertions`: the mandatory tags, the key resources and
   properties, and every configuration variable's effect (set with
   `monkeypatch.setenv`) including the rejected values. See
   [`docs/TESTING.md`](docs/TESTING.md).
8. **Coverage: never below 80%** (`make coverage`); `shared/`, `app.py`,
   `scripts/` and every `stack.py` are near 100% today - keep the code you
   add covered.

## 4. Do not invent things: the core guardrail

1. **Every construct, property, CLI command, metric name, default value and
   AWS behavior comes from a source actually consulted** - the
   [AWS CDK Python API reference](https://docs.aws.amazon.com/cdk/api/v2/python/)
   (or the construct library READMEs shipped with `aws-cdk-lib`), the AWS
   service documentation, the AWS CLI help (`aws <service> <command> help`),
   or the vendor's own documentation (floci, Prometheus, Grafana, Datadog,
   Docker Hub). Not memory.
2. **Verify by running.** A CLI command goes in a README after it ran (on
   floci, or it is marked real-AWS-only and taken from the official docs).
   A config file for a container (ADOT, Prometheus, Grafana, Caddy, nginx)
   was started with that exact image before being committed.
3. **floci behavior is observed, not assumed.** Every "floci vs real AWS"
   row comes from deploying and checking; when floci accepts something but
   doesn't do it, say so. floci changes quickly - name the version (2.1.0).
4. **Prefer L2 constructs**; use L1 `Cfn*` where no L2 exists (and say so in
   the README). **Never use an `-alpha` package**, and avoid features the
   CDK documents as experimental (e.g. `crossRegionReferences` - module 18
   passes values through variables instead).
5. **Numbers and versions are the riskiest content** (image tags, quotas,
   defaults, thresholds): check the current source and say they can change.
6. If a page won't load, try another official URL; if something stays
   unverified, leave it out or say explicitly that it is unverified.

## 5. Tagging and naming (non-negotiable)

```python
from shared.naming import bounded_name, resource_name
from shared.tagging import apply_name_tag, apply_standard_tags

apply_standard_tags(self, tags=config.to_standard_tags())     # once, after super().__init__

queue_name = resource_name(config.product, config.environment, "sqs", "billing")
queue = sqs.Queue(self, "BillingQueue", queue_name=queue_name)
apply_name_tag(queue, queue_name)

# 32-character limit (load balancers, target groups):
alb_name = bounded_name(config.product, config.environment, "alb", "web", max_length=32)
```

- Environments are `dev`, `stg`, `prd` only (`shared/config.py` rejects
  anything else).
- Never hardcode a tag value, product, environment or team name - they come
  from `config`. A module only invents the purpose segment of a name.
- A `Name` tag must not reference the resource's own attribute (e.g.
  `topic.topic_name`) - that is a circular dependency; tag with the name
  string.
- **Secrets referenced by name**: create the secret with a fixed
  `secret_name` and reference it by name (`shared/database.py`); floci
  rejects the `Fn::Join` dynamic references some CDK helpers build.

## 6. Account/region flexibility and configuration (non-negotiable)

- No hardcoded account, region or Availability Zone: `env=` comes from
  `shared.config.get_environment()`; AZs from `max_azs`.
- **Everything configurable is an environment variable with a default**,
  read with `env_str`/`env_int`/`env_bool` (`minimum=` for numbers) and
  validated at synth time with a message naming the variable. Shared knobs
  live in `shared/config.py` and `shared/ecs.py` (`REQUIREMENTS.md` section
  9.1); module knobs are `CDK_<MODULE>_*` and listed in the README.
- Listener ports come from `listener_port("<NAME>", default)`; on floci each
  listener needs its own port (`REQUIREMENTS.md` section 6) - add new ones
  to `.env.example` and that table. Two listeners on one load balancer need
  different defaults.
- floci-only workarounds are variables with real-AWS defaults, set in the
  floci block at the end of `.env.example` with a comment explaining why.

## 7. Applications, images and floci

- **Applications come from Docker Hub**, unmodified (official or vendor
  images: `nginx`, `traefik/whoami`, `caddy`, `postgrest/postgrest`,
  `wordpress`, `amazon/aws-cli`, `curlimages/curl`, `prom/prometheus`,
  `grafana/grafana`, `datadog/agent`, ...), pinned to a tag that was checked
  on Docker Hub. Configuration is passed as environment variables and
  written by the container's start command - no custom images.
- Shared images and commands live in `shared/apps.py`.
- Every module is deployed and exercised on floci before it is considered
  done (deploy, the Verify and CLI sections, destroy, prune).

## 8. Markdown formatting conventions

- A manual TOC between `<!-- TOC -->` markers at the top of every file with
  more than ~3 sections, one entry per `##`/`###` heading.
- Anchors follow GitHub's slug rule: lower case; remove everything except
  letters, digits, spaces, `-` and `_`; spaces become `-`. Re-check every
  anchor after renaming a heading.
- Relative links only (`../../REQUIREMENTS.md#9-flexible-configuration`).
- Tables for comparisons; every README ends with `## References` (official
  sources only).
- Commands are copy-pasteable, with the expected output as a comment when it
  helps; placeholders look like `<your-aws-cli-profile>`.

## 9. Python/CDK conventions

- `import aws_cdk as cdk`, `from aws_cdk import Duration, Stack`, and
  `from aws_cdk import aws_ecs as ecs` style aliases.
- `from __future__ import annotations` and type hints everywhere;
  `make typecheck` (mypy) must pass.
- `make lint` (ruff) must pass; lines up to the configured length.
- `scope` and `construct_id` are positional.
- Everything runs through `uv run`.

## 10. Workflow when adding or changing a module

1. Read `modules/01_network/` and `modules/02_fargate_service/`.
2. Look up the constructs, CLI commands and behaviors (section 4).
3. Write `stack.py` following the contract (section 3); `uv run cdk synth <StackId>`.
4. Deploy to floci, run every Verify/CLI command you will document, note
   every floci difference, destroy and prune.
5. Write the tests; `uv run pytest tests/unit/test_NN_topic.py -v`; make
   sure a test fails when you break what it checks.
6. Write the README; generate the resource listing (section 3, point 6).
7. `make test lint typecheck coverage`.
8. Update `docs/LEARNING-PATH.md`, `README.md`, `CHANGELOG.md`, and
   `docs/METRICS.md`/`docs/TROUBLESHOOTING.md` when the module adds metrics
   or failure modes.
9. Git: default branch `main`; **do not commit or push without an explicit
   request**.

## 11. What not to do

- Don't invent a construct, property, CLI flag, metric, default or behavior.
- Don't use an alpha package or an experimental CDK feature.
- Don't hardcode tags, names, accounts, regions, AZs or ports.
- Don't hand-write the "List every resource" commands.
- Don't document a CLI command you didn't run (or mark it real-AWS-only
  with its official source).
- Don't build custom images - use Docker Hub images and configuration.
- Don't drop below 80% coverage, or leave a broken anchor or stale TOC.
- Don't replace a documented long-form command with a `make` target - the
  targets are optional shortcuts (`REQUIREMENTS.md` section 5.4).
- Don't commit or push without being asked.

## 12. Environment notes

No CI pipeline is defined yet. Deploying needs Docker + Docker Compose v2
(or `floci-cli`), network access to Docker Hub (rate limits: `REQUIREMENTS.md`
section 2) and to PyPI on the first `uv sync`.
