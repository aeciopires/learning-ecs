<!-- TOC -->

- [Testing](#testing)
  - [Why test infrastructure code?](#why-test-infrastructure-code)
  - [How CDK unit tests work, in one paragraph](#how-cdk-unit-tests-work-in-one-paragraph)
  - [Running the tests](#running-the-tests)
  - [Where tests live](#where-tests-live)
  - [Reading a test, line by line](#reading-a-test-line-by-line)
  - [The `config` fixture and the environment](#the-config-fixture-and-the-environment)
  - [Testing configuration variables](#testing-configuration-variables)
  - [The assertion toolbox](#the-assertion-toolbox)
  - [Writing a test for a new module, step by step](#writing-a-test-for-a-new-module-step-by-step)
  - [What these tests do not check](#what-these-tests-do-not-check)
  - [Test coverage](#test-coverage)
  - [References](#references)

<!-- TOC -->

# Testing

How the unit tests in [`tests/`](../tests/) work, and how to write one.

## Why test infrastructure code?

A CDK stack is a program that produces a CloudFormation template. A typo in
a property, a forgotten security group rule or a variable that stopped
having an effect all synthesize happily - and fail (or silently do the
wrong thing) at deploy time, minutes later, possibly on a real account.
Unit tests catch those in about a second, without Docker, floci or AWS
credentials.

## How CDK unit tests work, in one paragraph

A test builds the stack in memory (`cdk.App()` + the module's `STACK_CLASS`),
turns it into a CloudFormation template with
`aws_cdk.assertions.Template.from_stack(stack)` - exactly what `cdk synth`
would write to `cdk.out/` - and asserts on that template: how many
resources of a type exist, which properties they have, what they reference.
Nothing is deployed. See
[Testing constructs](https://docs.aws.amazon.com/cdk/v2/guide/testing.html)
in the AWS CDK guide.

## Running the tests

```bash
uv run pytest                                   # every test (about 10 seconds)
uv run pytest tests/unit/test_04_alb.py -v      # one module, one line per test
uv run pytest -k circuit_breaker -v             # tests whose name matches
make test                                       # the same as `uv run pytest -q`
make coverage                                   # with a coverage report (section below)
```

## Where tests live

| File | Tests |
|---|---|
| `tests/unit/test_NN_topic.py` | one per module: its synthesized template |
| `tests/unit/test_app.py` | `app.py`: module discovery, `build_stacks()`, modules without `stack.py` |
| `tests/unit/test_shared_*.py` | `shared/config.py`, `naming.py`, `tagging.py` (and the knobs in `ecs.py`) |
| `tests/unit/test_resource_commands.py` | `scripts/resource_commands.py` - including "every resource type in every stack (and its variants) has a handler" |
| `tests/unit/test_floci_prune.py` | `scripts/floci_prune.py`, with botocore's `Stubber` (no network) |
| `tests/conftest.py` | the shared fixtures |
| `tests/_helpers.py` | `stack_class()` and `mandatory_tag_pairs()` |

Module directories start with a number (`04_alb`), which isn't a valid
Python identifier - `from modules.04_alb.stack import ...` is a syntax
error. `stack_class("04_alb")` imports the module by its string path (the
same way `app.py` does) and returns its `STACK_CLASS`.

## Reading a test, line by line

From [`tests/unit/test_14_sqs_sns.py`](../tests/unit/test_14_sqs_sns.py):

```python
SqsSnsStack = stack_class("14_sqs_sns")          # the module's STACK_CLASS


def _synth(config):
    app = cdk.App()                               # a fresh, in-memory CDK app
    stack = SqsSnsStack(app, "TestSqsSnsStack", config=config)
    return Template.from_stack(stack)             # the CloudFormation template, as a dict-like object


def test_consumers_scale_on_the_queue_backlog(config, monkeypatch):
    monkeypatch.setenv("CDK_SQS_MAX_CONSUMERS", "20")      # a module variable, for this test only
    template = _synth(config)
    template.resource_count_is("AWS::ApplicationAutoScaling::ScalableTarget", 2)
    template.has_resource_properties(                     # at least one resource matches...
        "AWS::ApplicationAutoScaling::ScalableTarget", {"MinCapacity": 1, "MaxCapacity": 20}
    )
```

`config` and `monkeypatch` are pytest **fixtures**: arguments pytest fills
in by name. `monkeypatch.setenv` sets an environment variable and undoes it
after the test.

## The `config` fixture and the environment

[`tests/conftest.py`](../tests/conftest.py) provides:

- **`config`** - a fixed `AppConfig` (product `learning-ecs`, environment
  `test`, team `platform-engineering`, not PCI, not cell-based). It never
  reads the environment, so a test's result doesn't depend on your shell.
- **an autouse fixture that removes every `CDK_*` variable** before each
  test. Modules read their knobs (`CDK_PORT_*`, `CDK_ALB_*`, ...) at synth
  time; with your `.env` loaded, a test would otherwise synthesize
  something else than it expects. A test that needs a variable sets it with
  `monkeypatch.setenv`.

## Testing configuration variables

Every `CDK_<MODULE>_*` variable a module documents gets a test that sets it
and asserts its effect - and every validation gets a test that it rejects
bad input:

```python
def test_unknown_strategy_is_rejected(config, monkeypatch):
    monkeypatch.setenv("CDK_DEPLOYMENT_STRATEGY", "big_bang")
    with pytest.raises(ValueError, match="CDK_DEPLOYMENT_STRATEGY"):
        _synth(config)
```

For several values, use `@pytest.mark.parametrize` (see
`test_traffic_shifting_strategies` in
[`test_17_deployments.py`](../tests/unit/test_17_deployments.py)).

## The assertion toolbox

| Call | Checks |
|---|---|
| `template.resource_count_is(type, n)` | exactly `n` resources of that type |
| `template.has_resource_properties(type, props)` | at least one resource of that type has these properties (others may exist) |
| `template.find_resources(type)` | returns `{logical_id: resource}` - assert on it in plain Python |
| `Match.object_like({...})` | a dict that contains at least these keys |
| `Match.array_with([...])` | a list that contains these items (in this order) |
| `Match.string_like_regexp("...")` | a string matching a regular expression |

Tags: CloudFormation sorts a resource's tags by key, so check each tag with
its own `Match.array_with([tag])` - `mandatory_tag_pairs(config)` returns
them ready to use.

When a value is a CloudFormation intrinsic (`{"Ref": ...}`,
`{"Fn::Join": ...}`) rather than a string, assert on the intrinsic or on
`json.dumps(...)` of it (see `_adot` in
[`test_20_prometheus_grafana.py`](../tests/unit/test_20_prometheus_grafana.py)).

## Writing a test for a new module, step by step

1. Copy the top of an existing test file (`stack_class`, `_synth`).
2. Synthesize with defaults; open `cdk.out/<StackId>.template.json` (after
   `uv run cdk synth <StackId>`) to see the exact property names.
3. Assert the resources that make the module what it is (a count, a key
   property, an IAM permission, a listener port).
4. One test per configuration variable and per validation.
5. The mandatory tags on the module's main resources.
6. **Break it on purpose**: change a property in `stack.py` and confirm a
   test fails. A test that can't fail isn't testing anything.
7. `uv run pytest tests/unit/test_NN_topic.py -v`, then `make coverage`.

## What these tests do not check

They prove the template says what you meant. They don't prove AWS (or
floci) accepts it, that the containers start, or that the application
works. That's what the floci deploy, the README's Verify section and
`make cdk-resources` are for - see
[`CLAUDE.md`, section 10](../CLAUDE.md#10-workflow-when-adding-or-changing-a-module).

## Test coverage

```bash
make coverage                          # report; fails below 80%
make coverage SKIP_COVERAGE_CHECK=1    # report only (work in progress)
```

`make coverage` runs
`uv run pytest tests --cov --cov-report=term-missing --cov-report=html`.
`[tool.coverage.*]` in [`pyproject.toml`](../pyproject.toml) sets what is
measured (`app`, `shared`, `modules`, `scripts`), turns on **branch
coverage** (an `if` counts only when both paths ran) and the 80% minimum.
`htmlcov/index.html` shows every line: green ran, red never ran, yellow a
branch that went one way only.

The rule: **never below 80% for the repository, and the code you add
covered** (the repository is at about 99% today). The only exclusions are
the `exclude_also` patterns in `pyproject.toml`; don't add
`# pragma: no cover` to code a test could reach.

## References

- [AWS CDK - Testing constructs](https://docs.aws.amazon.com/cdk/v2/guide/testing.html)
- [`aws_cdk.assertions` (Python)](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.assertions/README.html)
- [pytest - fixtures](https://docs.pytest.org/en/stable/explanation/fixtures.html) · [monkeypatch](https://docs.pytest.org/en/stable/how-to/monkeypatch.html) · [parametrize](https://docs.pytest.org/en/stable/how-to/parametrize.html)
- [coverage.py](https://coverage.readthedocs.io/) · [pytest-cov](https://pytest-cov.readthedocs.io/)
