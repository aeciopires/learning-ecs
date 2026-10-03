<!-- TOC -->

- [How this repository works](#how-this-repository-works)
  - [From a command to running containers](#from-a-command-to-running-containers)
  - [How app.py finds the modules](#how-apppy-finds-the-modules)
  - [Modules share code, not resources](#modules-share-code-not-resources)
  - [What each file in shared/ does](#what-each-file-in-shared-does)
  - [Where a setting comes from](#where-a-setting-comes-from)
  - [Names and tags](#names-and-tags)
  - [floci or real AWS: one variable decides](#floci-or-real-aws-one-variable-decides)
  - [Where a module's tests fit](#where-a-modules-tests-fit)
  - [References](#references)

<!-- TOC -->

# How this repository works

The picture behind the code: what happens when you type `uv run cdk
deploy`, how `app.py` finds the modules, what the modules share and what
they don't, and where every setting comes from. Read it once before
module 01; come back when something in a `stack.py` looks like magic.
The concepts (ECS, CDK, floci) are introduced in
[`REQUIREMENTS.md`, section 0](../REQUIREMENTS.md#0-zero-to-your-first-deploy-in-order).

## From a command to running containers

```mermaid
flowchart LR
    you(["you:<br/>uv run cdk deploy AlbStack"]) --> cli["CDK CLI<br/>(Node.js)"]
    env[".env<br/>CDK_* and AWS_* variables"] -.read by.-> app
    cli -- "runs cdk.json 'app':<br/>uv run python app.py" --> app["app.py<br/>+ modules/*/stack.py<br/>+ shared/*.py"]
    app -- "synthesizes" --> tpl[("cdk.out/<br/>AlbStack.template.json")]
    tpl -- "CloudFormation<br/>creates the resources" --> target{"AWS_ENDPOINT_URL<br/>set?"}
    target -- "yes" --> floci["floci<br/>localhost:4566"]
    target -- "no" --> aws["real AWS account<br/>(costs money)"]
    floci --> c1["ECS tasks as<br/>Docker containers<br/>on your machine"]
    aws --> c2["ECS tasks on<br/>Fargate or EC2"]
```

1. The CDK CLI reads [`cdk.json`](../cdk.json), whose `"app"` is
   `uv run python app.py`.
2. `app.py` builds one `AppConfig` from the environment, imports every
   module's `stack.py` and instantiates its stack. Nothing is created yet.
3. `app.synth()` writes one CloudFormation template per stack into
   `cdk.out/` - that is all `cdk synth` does.
4. `cdk deploy` sends that template to CloudFormation - floci's or your
   account's, depending on `AWS_ENDPOINT_URL`
   ([below](#floci-or-real-aws-one-variable-decides)) - which creates the
   VPC, the cluster, the services... and ECS starts the tasks.

## How app.py finds the modules

```mermaid
flowchart TD
    start(["app.py main()"]) --> cfg["config = load_app_config()<br/>env = get_environment()"]
    cfg --> loop["for each folder in modules/,<br/>in name order (01_..., 02_..., ...)"]
    loop --> has{"has a<br/>stack.py?"}
    has -- "no" --> loop
    has -- "yes" --> bs{"defines<br/>build_stacks()?"}
    bs -- "yes (module 18)" --> many["build_stacks(app, config, env)<br/>creates several stacks"]
    bs -- "no" --> one["STACK_CLASS(app, STACK_ID,<br/>config=config, env=env)"]
    many --> loop
    one --> loop
    loop -- "done" --> synth(["app.synth()<br/>-> cdk.out/*.template.json"])
```

This is why adding a module never means editing `app.py`: a new
`modules/NN_topic/stack.py` with `STACK_ID` and `STACK_CLASS` is found on
the next `uv run cdk list` (23 stacks today: one per module, three for
module 18). The full contract is in
[`CLAUDE.md`, section 3](../CLAUDE.md#3-the-module-contract).

## Modules share code, not resources

Every module is a **separate stack with its own VPC, cluster and
services**. No stack reads another stack's outputs, so you can deploy,
break and destroy any module on its own, in any order. What the modules
share is *code*: the helpers in `shared/` that build those resources the
same way everywhere.

```mermaid
flowchart TB
    subgraph code["Python code (shared at synth time)"]
        direction LR
        shared["shared/<br/>config · naming · tagging<br/>network · ecs · database · apps"]
        m01["01_network/stack.py"]
        m02["02_fargate_service/stack.py"]
        m04["04_alb/stack.py"]
        mxx["... 21_datadog/stack.py"]
        shared --> m01 & m02 & m04 & mxx
    end
    subgraph deployed["What gets deployed (independent stacks)"]
        direction LR
        s01["NetworkStack<br/>own VPC"]
        s02["FargateServiceStack<br/>own VPC + cluster"]
        s04["AlbStack<br/>own VPC + cluster + ALBs"]
        sxx["DatadogStack<br/>own VPC + cluster"]
    end
    m01 --> s01
    m02 --> s02
    m04 --> s04
    mxx --> sxx
```

So "module 04 builds on module 02" means you learn 02's service pattern
first - not that 02 must be deployed. The order to *learn* them is in
[`LEARNING-PATH.md`](LEARNING-PATH.md).

## What each file in shared/ does

```mermaid
flowchart LR
    tagging["tagging.py<br/>the 7 mandatory tags"]
    naming["naming.py<br/>resource_name()<br/>bounded_name()"]
    config["config.py<br/>AppConfig, env_str/int/bool,<br/>get_environment()"]
    network["network.py<br/>build_vpc(), task_subnets(),<br/>data_subnets()"]
    ecs["ecs.py<br/>build_cluster(), fargate_service(),<br/>log_driver(), listener_port()"]
    database["database.py<br/>generated_credentials(),<br/>password_reference()"]
    apps["apps.py<br/>Docker Hub images<br/>and commands"]
    config --> tagging
    network --> config & naming & tagging
    database --> config & naming & tagging
    ecs --> config & naming & network & tagging
```

An arrow means "imports". `apps.py` holds only constants (images, ports,
SQL) used by modules 09-11, 19 and 21.

| File | You use it when | Main entry points |
|---|---|---|
| [`config.py`](../shared/config.py) | reading any setting | `AppConfig`, `env_str`, `env_int`, `env_bool`, `get_environment` |
| [`naming.py`](../shared/naming.py) | naming a resource | `resource_name`, `bounded_name` (32-character limits) |
| [`tagging.py`](../shared/tagging.py) | tagging the stack and each resource | `apply_standard_tags`, `apply_name_tag` |
| [`network.py`](../shared/network.py) | creating the VPC, choosing subnets | `build_vpc`, `task_subnets`, `data_subnets` |
| [`ecs.py`](../shared/ecs.py) | creating clusters, services, log groups, listener ports | `build_cluster`, `fargate_service`, `log_driver`, `listener_port` |
| [`database.py`](../shared/database.py) | database credentials in Secrets Manager | `generated_credentials`, `password_reference` |
| [`apps.py`](../shared/apps.py) | an image or command used by several modules | constants |

`fargate_service()` is module 02's pattern in one function: task
definition, container, log group and service.
[`modules/02_fargate_service/stack.py`](../modules/02_fargate_service/stack.py)
writes every one of those pieces out by hand, so you can read what the
helper hides.

```mermaid
flowchart LR
    fn["fargate_service(scope, config, cluster,<br/>purpose='web', image='nginx:...')"] --> td["FargateTaskDefinition<br/>family {product}-{env}-web"]
    td --> ctr["container 'app'<br/>image from Docker Hub"]
    ctr --> lg["log group<br/>/ecs/{product}/{env}/web"]
    fn --> svc["FargateService<br/>{product}-{env}-web"]
    svc --> opts["task subnets, capacity providers,<br/>circuit breaker, ECS Exec,<br/>AZ rebalancing, tag propagation"]
```

## Where a setting comes from

Every knob is an environment variable with a default, read **at synth
time**. A bad value stops `cdk synth` with a message naming the variable,
before anything reaches CloudFormation.

```mermaid
flowchart LR
    file[".env<br/>(copied from .env.example)"] -- "set -a; source .env; set +a" --> shell["your shell's<br/>environment"]
    shell --> reader{"env_str / env_int / env_bool<br/>(shared/config.py)"}
    reader -- "unset or empty" --> def["the default<br/>in the code"]
    reader -- "valid value" --> val["your value"]
    reader -- "invalid value" --> err["ValueError naming the variable<br/>-> cdk synth fails"]
    def & val --> stack["the stack being synthesized"]
```

- Shared knobs (`CDK_PRODUCT`, `CDK_VPC_CIDR`, `CDK_NAT_GATEWAYS`, ...):
  [`REQUIREMENTS.md`, section 9.1](../REQUIREMENTS.md#91---variables-every-module-reads).
- A module's own knobs (`CDK_<MODULE>_*`): the "Configuration" table of
  its README.
- Remember to `source .env` again in every new terminal, and after
  editing it.

## Names and tags

Names and tag values come from `config`, never from a literal in a
module. A module only chooses the *type* and *purpose* segments of a name.

```mermaid
flowchart LR
    p["CDK_PRODUCT<br/>learning-ecs"] --> rn
    e["CDK_ENVIRONMENT<br/>dev | stg | prd"] --> rn
    t["type (module)<br/>e.g. alb"] --> rn
    u["purpose (module)<br/>e.g. public"] --> rn
    rn["resource_name() /<br/>bounded_name(max_length=32)"] --> name["learning-ecs-dev-alb-public"]
    name --> res["the resource's own name<br/>(load_balancer_name=...)"]
    name --> tag["its Name tag<br/>apply_name_tag()"]
    cfg["AppConfig.to_standard_tags()"] --> std["apply_standard_tags(stack):<br/>environment, product, team-owner,<br/>pci, cell-based (+ cell-id)"]
    std --> all["every taggable resource<br/>in the stack"]
```

`bounded_name()` shortens a name that would exceed a limit (load
balancers and target groups: 32 characters) - the details and the full
policy are in
[`REQUIREMENTS.md`, sections 7 and 8](../REQUIREMENTS.md#7-tagging-policy).

## floci or real AWS: one variable decides

The same code, the same template, two destinations. The AWS CLI, boto3
and the CDK CLI all send their API calls to `AWS_ENDPOINT_URL` when it is
set.

```mermaid
flowchart TB
    tpl[("the same template<br/>cdk.out/{StackId}.template.json")]
    tpl --> q{"AWS_ENDPOINT_URL"}
    q -- "http://localhost:4566<br/>(.env.example)" --> f
    q -- "unset" --> a
    subgraph f["floci (free)"]
        direction TB
        f1["source .env<br/>(endpoint + floci-only overrides<br/>from the end of .env.example)"] --> f2["cdk deploy --method=direct"]
        f2 --> f3["tasks = Docker containers;<br/>load balancers on localhost:8080-8099"]
        f3 --> f4["cdk destroy, then<br/>scripts/floci_prune.py --apply"]
    end
    subgraph a["real AWS (billed)"]
        direction TB
        a1["unset the floci variables;<br/>use your AWS profile"] --> a2["cdk deploy<br/>(default method: change sets)"]
        a2 --> a3["tasks on Fargate/EC2;<br/>load balancers on AWS DNS names"]
        a3 --> a4["cdk destroy right after"]
    end
```

How floci runs what you deploy - its ports, its network and the task
containers - is drawn in
[`REQUIREMENTS.md`, section 5](../REQUIREMENTS.md#5-running-floci-the-local-aws-emulator);
what it emulates and what it doesn't is in
[section 10](../REQUIREMENTS.md#10-floci-vs-real-aws).

## Where a module's tests fit

Unit tests synthesize a stack in memory and check the template - no
floci, no AWS, no credentials. They run the same `stack.py` that `cdk
deploy` does, so a broken setting or a missing tag fails `uv run pytest`
before it reaches floci. How they work, with a diagram, and a
step-by-step for a new module: [`TESTING.md`](TESTING.md#how-cdk-unit-tests-work-in-one-paragraph).

## References

- [AWS CDK - Apps](https://docs.aws.amazon.com/cdk/v2/guide/apps.html)
- [AWS CDK - Environments](https://docs.aws.amazon.com/cdk/v2/guide/environments.html)
- [AWS CDK - Tagging](https://docs.aws.amazon.com/cdk/v2/guide/tagging.html)
- [AWS CDK - Testing constructs](https://docs.aws.amazon.com/cdk/v2/guide/testing.html)
- [AWS CLI - Configuring endpoints (`AWS_ENDPOINT_URL`)](https://docs.aws.amazon.com/sdkref/latest/guide/feature-ss-endpoints.html)
- [floci](https://floci.io)
