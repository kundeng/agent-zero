"""Tests for the BaseProvisioner registry + WizardStep/StepResult JSON (spec 08 task 2.3).

Covers the framework primitives in isolation from any per-platform
provisioner. Builds a stub provisioner inline, drives the registry,
checks the JSON round-trips, and asserts the bookkeeping invariants
that downstream code depends on.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyperagent0.channels.provision.base import (
    BaseProvisioner,
    ProvisionContext,
    StepResult,
    WizardField,
    WizardStep,
    _PROVISIONER_REGISTRY,
    get_provisioner,
    register_provisioner,
    registered_provisioners,
    step_result_to_json,
)


class _StubProvisioner(BaseProvisioner):
    channel_type = "stub-test"
    required_secrets = ["STUB_TOKEN"]
    bootstrap_url = "https://example.com/stub"

    def wizard_steps(self):
        return [
            WizardStep(
                id="enter",
                kind="input",
                label="Enter token",
                fields=[WizardField(id="token", label="Token", secret=True)],
            ),
        ]

    def provision(self, step_id, inputs, ctx):
        return StepResult(terminal=True, message="ok")

    def oauth_callback(self, query, ctx):
        return StepResult(error="not supported")

    def test_connection(self, ctx):
        return "ok"

    def channels_json_block(self, ctx):
        return {"enabled": True}


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot + restore the registry around each test."""

    snapshot = dict(_PROVISIONER_REGISTRY)
    yield
    _PROVISIONER_REGISTRY.clear()
    _PROVISIONER_REGISTRY.update(snapshot)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_register_and_get_provisioner():
    register_provisioner("stub-test", _StubProvisioner)
    assert get_provisioner("stub-test") is _StubProvisioner
    assert "stub-test" in registered_provisioners()


def test_registered_provisioners_returns_sorted_unique():
    register_provisioner("zebra", _StubProvisioner)
    register_provisioner("alpha", _StubProvisioner)
    names = registered_provisioners()
    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_unknown_provisioner_returns_none():
    assert get_provisioner("no-such-platform") is None


def test_re_register_replaces_class():
    """Tests use this seam to swap in stub provisioners over real ones."""

    class _Other(_StubProvisioner):
        channel_type = "stub-test"

    register_provisioner("stub-test", _StubProvisioner)
    register_provisioner("stub-test", _Other)
    assert get_provisioner("stub-test") is _Other


def test_register_empty_name_raises():
    with pytest.raises(ValueError):
        register_provisioner("", _StubProvisioner)


def test_register_non_string_name_raises():
    with pytest.raises(TypeError):
        register_provisioner(123, _StubProvisioner)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# WizardStep / WizardField JSON round-trip
# ---------------------------------------------------------------------------


def test_wizard_step_to_json_minimal():
    s = WizardStep(id="x", kind="info", label="Hi")
    out = s.to_json()
    assert out["id"] == "x"
    assert out["kind"] == "info"
    assert out["fields"] == []
    assert out["timeout_s"] == 90
    assert out["next_on_success"] is None


def test_wizard_step_to_json_with_fields():
    s = WizardStep(
        id="creds",
        kind="input",
        label="Enter creds",
        fields=[
            WizardField(id="user", label="Username"),
            WizardField(id="pw", label="Password", kind="password", secret=True),
        ],
    )
    out = s.to_json()
    assert [f["id"] for f in out["fields"]] == ["user", "pw"]
    assert out["fields"][1]["secret"] is True
    assert out["fields"][1]["kind"] == "password"


def test_wizard_field_select_options():
    f = WizardField(
        id="region",
        label="Region",
        kind="select",
        options=[{"value": "us", "label": "US"}, {"value": "eu", "label": "EU"}],
    )
    out = f.to_json()
    assert len(out["options"]) == 2
    assert out["options"][0]["value"] == "us"


# ---------------------------------------------------------------------------
# StepResult JSON
# ---------------------------------------------------------------------------


def test_step_result_to_json_terminal():
    r = StepResult(terminal=True, message="done")
    parsed = json.loads(step_result_to_json(r))
    assert parsed["terminal"] is True
    assert parsed["next_step"] is None
    assert parsed["error"] is None


def test_step_result_to_json_error_with_pointer():
    r = StepResult(error="bad scope", error_pointer="/oauth_config/scopes/bot/0")
    parsed = json.loads(step_result_to_json(r))
    assert parsed["error"] == "bad scope"
    assert parsed["error_pointer"] == "/oauth_config/scopes/bot/0"


def test_step_result_to_json_advance():
    r = StepResult(
        next_step="install",
        message="click to install",
        url_override="https://slack.com/...",
        state_token="abc",
        extra={"app_id": "A123"},
    )
    parsed = json.loads(step_result_to_json(r))
    assert parsed["next_step"] == "install"
    assert parsed["url_override"] == "https://slack.com/..."
    assert parsed["state_token"] == "abc"
    assert parsed["extra"]["app_id"] == "A123"
