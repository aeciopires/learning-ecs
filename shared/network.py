"""The VPC every module runs its ECS tasks in - built one way, everywhere.

Every module builds its own VPC (so each one deploys and destroys on its
own), but always through `build_vpc()`, so the shape is identical and is
controlled from .env, never from a module:

    CDK_VPC_CIDR      the VPC's IPv4 range              (default 10.0.0.0/16)
    CDK_MAX_AZS       how many Availability Zones       (default 2 - "multi-AZ")
    CDK_NAT_GATEWAYS  NAT Gateways (0 = none, cheaper)   (default 1)

Subnet tiers (one subnet per tier per AZ), as in the AWS reference
architecture for ECS - see modules/01_network/README.md:

    public    load balancers, NAT Gateways (and tasks when CDK_TASK_SUBNETS=public)
    private   ECS tasks - reach the internet only through a NAT Gateway
    isolated  databases and caches - no route to the internet at all

References:
- https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ec2/Vpc.html
- https://docs.aws.amazon.com/AmazonECS/latest/developerguide/networking-outbound.html
"""

from __future__ import annotations

from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from shared.config import AppConfig
from shared.naming import resource_name
from shared.tagging import apply_name_tag

# Subnet group names - also how other code selects them (SubnetSelection(subnet_group_name=...)).
PUBLIC = "public"
PRIVATE = "private"
ISOLATED = "isolated"


def build_vpc(scope: Construct, config: AppConfig, purpose: str, *, isolated: bool = False) -> ec2.Vpc:
    """A VPC with public + private (+ isolated, when `isolated`) subnets.

    Without a NAT Gateway (`CDK_NAT_GATEWAYS=0`) there is no `private` tier:
    the CDK refuses PRIVATE_WITH_EGRESS subnets with no NAT to route
    through, and a task there could not pull its Docker Hub image anyway -
    tasks then run in the public subnets (see `AppConfig.tasks_in_public_subnets`).
    """
    subnets = [ec2.SubnetConfiguration(name=PUBLIC, subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)]
    if config.nat_gateways > 0:
        subnets.append(
            ec2.SubnetConfiguration(name=PRIVATE, subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=22)
        )
    if isolated:
        subnets.append(
            ec2.SubnetConfiguration(name=ISOLATED, subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24)
        )

    vpc_name = resource_name(config.product, config.environment, "vpc", purpose)
    vpc = ec2.Vpc(
        scope,
        "Vpc",
        vpc_name=vpc_name,
        ip_addresses=ec2.IpAddresses.cidr(config.vpc_cidr),
        max_azs=config.max_azs,
        nat_gateways=min(config.nat_gateways, config.max_azs),
        subnet_configuration=subnets,
    )
    apply_name_tag(vpc, vpc_name)
    return vpc


def task_subnets(config: AppConfig) -> ec2.SubnetSelection:
    """Where a module places its ECS tasks - see `AppConfig.tasks_in_public_subnets`."""
    return ec2.SubnetSelection(subnet_group_name=PUBLIC if config.tasks_in_public_subnets else PRIVATE)


def data_subnets() -> ec2.SubnetSelection:
    """Where a module places its databases/caches: the isolated tier."""
    return ec2.SubnetSelection(subnet_group_name=ISOLATED)
