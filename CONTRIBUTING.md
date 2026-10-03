<!-- TOC -->

- [Contributing](#contributing)
  - [Ground rules](#ground-rules)
  - [Development workflow](#development-workflow)
  - [Adding a new module](#adding-a-new-module)
  - [Updating an image tag or the floci version](#updating-an-image-tag-or-the-floci-version)
  - [Opening a pull request](#opening-a-pull-request)
  - [Reporting issues](#reporting-issues)

<!-- TOC -->

# Contributing

Thanks for your interest! This is a beginner-oriented learning path for
Amazon ECS with the AWS CDK in Python. Contributions of all sizes are
welcome: a typo, a clearer explanation, a missing CLI command, a fix to a
stack, a new floci behavior you observed, or a new module.

## Ground rules

1. **Deploy against floci first.** Every module's primary instructions
   target the [floci](https://floci.io) emulator; "Deploy to real AWS" is
   optional and states the cost - see [`REQUIREMENTS.md`](REQUIREMENTS.md).
2. **Don't invent facts.** Constructs, properties, CLI commands, metric
   names, defaults and behaviors come from a source you consulted, and CLI
   commands were run - see
   [`CLAUDE.md`, section 4](CLAUDE.md#4-do-not-invent-things-the-core-guardrail).
3. **Tagging, naming and configuration**: the 7 tags (`shared/tagging.py`),
   names from `shared/naming.py`, every knob an environment variable with a
   default - see [`CLAUDE.md`, sections 5-6](CLAUDE.md#5-tagging-and-naming-non-negotiable).
4. **Docker Hub images, unmodified** - see
   [`CLAUDE.md`, section 7](CLAUDE.md#7-applications-images-and-floci).
5. **Match the module contract** - [`CLAUDE.md`, section 3](CLAUDE.md#3-the-module-contract).

## Development workflow

```bash
make check                                   # OS + every required tool
uv sync                                      # dependencies
docker compose up -d floci                   # local AWS emulator
set -a; source .env; set +a                  # point the CLI/CDK at floci (cp .env.example .env first)
uv run cdk bootstrap                         # once per floci instance
uv run cdk synth <StackId>
uv run cdk diff <StackId>
uv run cdk deploy <StackId> --require-approval never --method=direct
make cdk-resources STACK=<StackId>           # the README's resource listing, run for real
uv run cdk destroy <StackId>
uv run python scripts/floci_prune.py --apply
uv run pytest tests/unit/test_NN_topic.py -v # that module's tests
make test lint typecheck coverage            # everything; coverage must stay >= 80%
```

The `make cdk-*`/`floci-*` targets are optional shortcuts
([`REQUIREMENTS.md`, section 5.4](REQUIREMENTS.md#54---optional-make-shortcuts-for-floci-and-the-cdk));
documentation keeps the long forms. How the tests work:
[`docs/TESTING.md`](docs/TESTING.md).

## Adding a new module

1. Read [`CLAUDE.md`](CLAUDE.md), then `modules/01_network/` and
   `modules/02_fargate_service/` (stack, README, test).
2. Look up every construct and command you plan to use.
3. Create `modules/NN_topic/` (next free number) with `__init__.py`,
   `stack.py`, `README.md`.
4. Deploy it to floci and run every command you'll document; note each
   floci difference.
5. Write `tests/unit/test_NN_topic.py`; generate the README's resource
   listing with `scripts/resource_commands.py --markdown ... --readme ...`
   ([`REQUIREMENTS.md`, section 5.8](REQUIREMENTS.md#58---listing-every-resource-a-stack-created)).
6. New listener ports go into `.env.example` and
   [`REQUIREMENTS.md`, section 6](REQUIREMENTS.md#6-network-ports-used);
   new floci gaps into section 10.
7. Add the module to [`docs/LEARNING-PATH.md`](docs/LEARNING-PATH.md) and
   [`README.md`](README.md), its metrics to
   [`docs/METRICS.md`](docs/METRICS.md), its failure modes to
   [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md), and an entry to
   [`CHANGELOG.md`](CHANGELOG.md) under "Unreleased".

## Updating an image tag or the floci version

- **An image tag**: check the tag on Docker Hub, change the constant (most
  live in the module's `stack.py` or `shared/apps.py`), update the tests
  that assert it, deploy the module to floci and re-run its README commands.
- **floci**: deploy every module with the new release, re-check each
  "floci vs real AWS" table and `REQUIREMENTS.md` section 10, and update
  `FLOCI_NOT_CREATED`/`FLOCI_NOT_LISTABLE` in
  `scripts/resource_commands.py`, then regenerate the listings. Note the
  version you tested in `CHANGELOG.md`.

## Opening a pull request

1. Fork and branch from `main`.
2. Follow the ground rules; run the checks above for every module you touched.
3. Describe *why* the change is useful and link the official sources you
   relied on.

## Reporting issues

Open a GitHub issue with the module (e.g. `modules/04_alb`), the exact
command, the full output, and whether it was floci (which version) or real
AWS.
