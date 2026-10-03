"""Shared pytest fixtures for every module's unit tests.

See docs/TESTING.md for the full beginner explanation of how these tests
work, how to run them, and how to write a new one.
"""

from __future__ import annotations

import os

# Silences a harmless JSII/Node.js compatibility warning that would
# otherwise print to stderr on every test run - see the AWS CDK's jsii
# runtime docs. Set before aws_cdk is imported anywhere.
os.environ.setdefault("JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION", "1")

import pytest

from shared.config import AppConfig


@pytest.fixture(autouse=True)
def _no_cdk_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every CDK_* variable before each test.

    Modules read their own knobs (CDK_PORT_*, CDK_VPC_FLOW_LOGS, ...) from
    the environment at synth time, so a `.env` loaded in your shell would
    otherwise change what a test synthesizes. A test that needs one sets it
    with `monkeypatch.setenv(...)`.
    """
    for name in list(os.environ):
        if name.startswith("CDK_"):
            monkeypatch.delenv(name)


@pytest.fixture
def config() -> AppConfig:
    """A fixed, deterministic AppConfig, shared by every module's tests.

    Unlike `shared.config.load_app_config()` (what `app.py` uses, which
    reads real `CDK_*` environment variables - see REQUIREMENTS.md section
    9), this fixture never reads the environment: every test in this
    repository builds its stack against the exact same tag values, so a
    test's result never depends on what happens to be exported in your
    shell.
    """
    return AppConfig(
        product="learning-ecs",
        environment="test",
        team_owner="platform-engineering",
        pci=False,
        cell_based=False,
        cell_id=None,
    )
