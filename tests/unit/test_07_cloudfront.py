"""Unit tests for modules/07_cloudfront. See docs/TESTING.md for how these work."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from tests._helpers import mandatory_tag_pairs, stack_class

CloudFrontStack = stack_class("07_cloudfront")

CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"


def _synth(config):
    app = cdk.App()
    stack = CloudFrontStack(app, "TestCloudFrontStack", config=config)
    return Template.from_stack(stack)


def _distribution_config(template):
    resources = template.find_resources("AWS::CloudFront::Distribution")
    assert len(resources) == 1
    return next(iter(resources.values()))["Properties"]["DistributionConfig"]


def test_public_alb_origin_with_a_secret_header(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer", {"Scheme": "internet-facing", "Type": "application"}
    )
    origin = _distribution_config(template)["Origins"][0]
    assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "http-only"
    header = origin["OriginCustomHeaders"][0]
    assert header["HeaderName"] == "X-Origin-Verify"
    # The value is a dynamic reference to the secret, never the secret itself.
    assert "resolve:secretsmanager" in str(header["HeaderValue"])


def test_alb_forwards_only_requests_with_the_header_and_403s_the_rest(config):
    template = _synth(config)
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        {
            "Conditions": [Match.object_like({"Field": "http-header", "HttpHeaderConfig": Match.object_like({"HttpHeaderName": "X-Origin-Verify"})})],
            "Actions": [Match.object_like({"Type": "forward"})],
        },
    )
    template.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::Listener",
        {"DefaultActions": [Match.object_like({"Type": "fixed-response", "FixedResponseConfig": Match.object_like({"StatusCode": "403"})})]},
    )


def test_default_behavior_does_not_cache_and_static_does(config):
    distribution = _distribution_config(_synth(config))
    assert distribution["DefaultCacheBehavior"]["CachePolicyId"] == CACHING_DISABLED
    assert distribution["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    static = distribution["CacheBehaviors"][0]
    assert static["PathPattern"] == "/static/*"
    assert static["CachePolicyId"] == CACHING_OPTIMIZED
    assert distribution["PriceClass"] == "PriceClass_100"


def test_prefix_list_restricts_the_alb_security_group(config, monkeypatch):
    monkeypatch.setenv("CDK_CLOUDFRONT_PREFIX_LIST_ID", "pl-00000000")
    template = _synth(config)
    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress", {"SourcePrefixListId": "pl-00000000", "FromPort": 80}
    )
    # ...and no rule open to the whole internet on the ALB.
    for resource in template.find_resources("AWS::EC2::SecurityGroup").values():
        for rule in resource["Properties"].get("SecurityGroupIngress", []):
            assert rule.get("CidrIp") != "0.0.0.0/0"


def test_vpc_origin_uses_an_internal_alb(config, monkeypatch):
    monkeypatch.setenv("CDK_CLOUDFRONT_ORIGIN", "vpc_origin")
    monkeypatch.setenv("CDK_CLOUDFRONT_PREFIX_LIST_ID", "pl-00000000")
    template = _synth(config)
    template.resource_count_is("AWS::CloudFront::VpcOrigin", 1)
    template.has_resource_properties("AWS::ElasticLoadBalancingV2::LoadBalancer", {"Scheme": "internal"})
    template.resource_count_is("AWS::SecretsManager::Secret", 0)


def test_vpc_origin_requires_the_prefix_list(config, monkeypatch):
    monkeypatch.setenv("CDK_CLOUDFRONT_ORIGIN", "vpc_origin")
    with pytest.raises(ValueError, match="CDK_CLOUDFRONT_PREFIX_LIST_ID"):
        _synth(config)


def test_origin_domain_override_for_floci(config, monkeypatch):
    monkeypatch.setenv("CDK_CLOUDFRONT_ORIGIN_DOMAIN", "localhost")
    origin = _distribution_config(_synth(config))["Origins"][0]
    assert origin["DomainName"] == "localhost"


@pytest.mark.parametrize(("variable", "value"), [("CDK_CLOUDFRONT_ORIGIN", "s3"), ("CDK_CLOUDFRONT_PRICE_CLASS", "999")])
def test_invalid_choices_are_rejected(config, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        _synth(config)


def test_distribution_has_the_mandatory_tags(config):
    template = _synth(config)
    for tag in mandatory_tag_pairs(config):
        template.has_resource_properties("AWS::CloudFront::Distribution", {"Tags": Match.array_with([tag])})
