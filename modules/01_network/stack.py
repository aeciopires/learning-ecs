"""Module 01 - Network: the multi-AZ VPC every ECS module in this path runs in.

AWS docs used while writing this module:
- Vpc construct: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ec2/Vpc.html
- FlowLog / FlowLogDestination: https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_ec2/FlowLogDestination.html
- Gateway endpoints: https://docs.aws.amazon.com/vpc/latest/privatelink/gateway-endpoints.html
- Amazon ECS - networking best practices (connecting to the internet):
  https://docs.aws.amazon.com/AmazonECS/latest/developerguide/networking-outbound.html
- VPC Flow Logs: https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs.html

See README.md in this directory for the full explanation and deploy steps.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_logs as logs
from constructs import Construct

from shared.config import AppConfig, env_bool
from shared.ecs import retention
from shared.naming import resource_name
from shared.network import build_vpc
from shared.tagging import apply_name_tag, apply_standard_tags

STACK_ID = "NetworkStack"


class NetworkStack(Stack):
    """A VPC with public, private and isolated subnets in `CDK_MAX_AZS` AZs.

    Every other module builds the very same VPC through `shared.network.build_vpc()`
    - this module exists to look at that VPC on its own, before any ECS
    resource is added on top. Two optional extras, both off the critical path
    of every other module:

    - `CDK_VPC_FLOW_LOGS=true` - VPC Flow Logs to CloudWatch Logs (the first
      tool to reach for when a task "can't connect" - see docs/TROUBLESHOOTING.md).
    - `CDK_VPC_S3_ENDPOINT=true` (default) - an S3 *gateway* endpoint, which
      has no hourly charge, so traffic from tasks to S3 skips the NAT Gateway.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        apply_standard_tags(self, tags=config.to_standard_tags())

        self.vpc = build_vpc(self, config, "network", isolated=True)

        if env_bool("CDK_VPC_S3_ENDPOINT", True):
            self.vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)

        if env_bool("CDK_VPC_FLOW_LOGS", False):
            log_group_name = f"/vpc/{config.product}/{config.environment}/flow-logs"
            flow_log_group = logs.LogGroup(
                self,
                "FlowLogGroup",
                log_group_name=log_group_name,
                retention=retention(config),
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            apply_name_tag(flow_log_group, log_group_name)
            flow_log = self.vpc.add_flow_log(
                "FlowLog",
                destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_log_group),
                traffic_type=ec2.FlowLogTrafficType.ALL,
            )
            apply_name_tag(flow_log, resource_name(config.product, config.environment, "flow-log", "network"))

        cdk.CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        cdk.CfnOutput(
            self,
            "PrivateSubnetIds",
            value=cdk.Fn.join(",", [s.subnet_id for s in self.vpc.private_subnets]) if self.vpc.private_subnets else "-",
        )
        cdk.CfnOutput(self, "PublicSubnetIds", value=cdk.Fn.join(",", [s.subnet_id for s in self.vpc.public_subnets]))
        cdk.CfnOutput(self, "IsolatedSubnetIds", value=cdk.Fn.join(",", [s.subnet_id for s in self.vpc.isolated_subnets]))


STACK_CLASS = NetworkStack
