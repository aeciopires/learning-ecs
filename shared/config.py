"""Account/region, tag values and deployment knobs - kept out of every stack.

Every value here is read from the environment so the same code deploys to
any AWS account/region - and to floci - without editing a single file. See
REQUIREMENTS.md ("Flexible configuration") and the AWS CDK "Environments"
guide: https://docs.aws.amazon.com/cdk/v2/guide/environments.html

Two kinds of settings live here:

* `AppConfig` - the values *every* stack needs (tags, network shape, logging,
  task placement). Built once in app.py by `load_app_config()`.
* `env_str()` / `env_int()` / `env_bool()` - typed readers for
  a module's own `CDK_*` knobs (an image tag, a desired count, an engine
  version, ...). Each module documents the variables it reads in its README
  and in .env.example.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import aws_cdk as cdk

from shared.tagging import StandardTags

# The only accepted values for the `environment` tag and the environment
# segment of every resource name - see REQUIREMENTS.md, "Tagging policy".
ENVIRONMENTS: tuple[str, ...] = ("dev", "stg", "prd")

_LONG_ENVIRONMENT_NAMES = {
    "development": "dev",
    "staging": "stg",
    "stage": "stg",
    "prod": "prd",
    "production": "prd",
}

# Where ECS tasks run: "private" subnets (reach the internet through a NAT
# Gateway) or "public" subnets (a public IP per task, no NAT needed).
TASK_SUBNET_TYPES: tuple[str, ...] = ("private", "public")

# CloudWatch Logs only accepts these retention values (days) - see the
# PutRetentionPolicy API reference:
# https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutRetentionPolicy.html
LOG_RETENTION_DAYS: tuple[int, ...] = (
    1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731,
    1096, 1827, 2192, 2557, 2922, 3288, 3653,
)


def validate_environment(name: str) -> str:
    """Return `name` normalized (stripped, lower-cased) if it is in `ENVIRONMENTS`.

    Raises `ValueError` otherwise, suggesting the short name when `name` is
    a common long form (e.g. "staging" -> "stg", "prod" -> "prd").
    """
    normalized = name.strip().lower()
    if normalized in ENVIRONMENTS:
        return normalized
    hint = _LONG_ENVIRONMENT_NAMES.get(normalized)
    suggestion = f' - did you mean "{hint}"?' if hint else ""
    raise ValueError(
        f"Invalid environment {name!r}: must be one of {', '.join(ENVIRONMENTS)}{suggestion}"
    )


def env_str(name: str, default: str) -> str:
    """The environment variable `name`, stripped, or `default` when unset/empty."""
    value = os.getenv(name, "").strip()
    return value or default


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """The environment variable `name` as an int, or `default` when unset/empty.

    Raises `ValueError` (naming the variable) when it isn't an integer or is
    below `minimum`, so a typo in .env fails at `cdk synth`, not mid-deploy.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"{name}={value} must be >= {minimum}")
    return value


def env_bool(name: str, default: bool) -> bool:
    """The environment variable `name` as a bool ("true"/"false"), or `default`."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ValueError(f"{name}={raw!r} must be true or false")


def get_environment() -> cdk.Environment | None:
    """Build a `cdk.Environment` from the standard CDK/AWS CLI env vars.

    - `CDK_DEFAULT_ACCOUNT` / `CDK_DEFAULT_REGION` are set automatically by
      the CDK CLI from your current AWS CLI credentials/profile when you run
      `cdk synth`/`cdk deploy` (see the CDK "Environments" guide above).
    - `AWS_ACCOUNT_ID` / `AWS_REGION` are accepted as explicit overrides.

    Returns `None` (an "environment-agnostic" stack) when nothing is set, so
    `cdk synth` and the unit tests work without any AWS credentials.
    """
    account = os.getenv("CDK_DEFAULT_ACCOUNT") or os.getenv("AWS_ACCOUNT_ID")
    region = os.getenv("CDK_DEFAULT_REGION") or os.getenv("AWS_REGION")
    if not account and not region:
        return None
    return cdk.Environment(account=account, region=region)


@dataclass(frozen=True)
class AppConfig:
    """Values shared by every stack, resolved once in app.py.

    The first six fields map to the mandatory tags in `shared/tagging.py`;
    the rest shape the network, the tasks and their logs. Override any of
    them with the matching `CDK_*` environment variable - see .env.example.
    """

    product: str
    environment: str
    team_owner: str
    pci: bool
    cell_based: bool
    cell_id: str | None
    # --- network (shared/network.py) ---
    vpc_cidr: str = "10.0.0.0/16"
    max_azs: int = 2
    nat_gateways: int = 1
    # --- ECS tasks (shared/ecs.py) ---
    task_subnets: str = "private"
    log_retention_days: int = 7

    def __post_init__(self) -> None:
        if self.task_subnets not in TASK_SUBNET_TYPES:
            raise ValueError(
                f"task_subnets={self.task_subnets!r}: must be one of {', '.join(TASK_SUBNET_TYPES)}"
            )
        if self.log_retention_days not in LOG_RETENTION_DAYS:
            raise ValueError(
                f"log_retention_days={self.log_retention_days} is not a value CloudWatch Logs "
                f"accepts: {', '.join(str(d) for d in LOG_RETENTION_DAYS)}"
            )
        if self.max_azs < 1:
            raise ValueError("max_azs must be >= 1")
        if self.nat_gateways < 0:
            raise ValueError("nat_gateways must be >= 0")

    @property
    def tasks_in_public_subnets(self) -> bool:
        """True when tasks must run in public subnets with a public IP.

        Always true without a NAT Gateway: a task in a private subnet could
        not reach Docker Hub to pull its image.
        """
        return self.task_subnets == "public" or self.nat_gateways == 0

    def to_standard_tags(self) -> StandardTags:
        return StandardTags(
            product=self.product,
            environment=self.environment,
            team_owner=self.team_owner,
            pci=self.pci,
            cell_based=self.cell_based,
            cell_id=self.cell_id,
        )


def load_app_config() -> AppConfig:
    cell_based = env_bool("CDK_CELL_BASED", False)
    return AppConfig(
        product=env_str("CDK_PRODUCT", "learning-ecs"),
        environment=validate_environment(env_str("CDK_ENVIRONMENT", "dev")),
        team_owner=env_str("CDK_TEAM_OWNER", "platform-engineering"),
        pci=env_bool("CDK_PCI", False),
        cell_based=cell_based,
        cell_id=(os.getenv("CDK_CELL_ID") or None) if cell_based else None,
        vpc_cidr=env_str("CDK_VPC_CIDR", "10.0.0.0/16"),
        max_azs=env_int("CDK_MAX_AZS", 2, minimum=1),
        nat_gateways=env_int("CDK_NAT_GATEWAYS", 1, minimum=0),
        task_subnets=env_str("CDK_TASK_SUBNETS", "private").lower(),
        log_retention_days=env_int("CDK_LOG_RETENTION_DAYS", 7, minimum=1),
    )
