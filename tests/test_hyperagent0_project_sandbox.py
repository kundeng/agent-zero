"""Tests for per-project sandbox resolution (spec 01-host-first task 1.6)."""

import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _import_projects_module():
    try:
        return importlib.import_module("python.helpers.projects")
    except ModuleNotFoundError as e:
        pytest.skip(f"python.helpers.projects unavailable: {e}")


def test_resolve_inherit_returns_global():
    _import_projects_module()
    from hyperagent0.projects.resolve import resolve_sandbox_mode

    with patch("python.helpers.projects.load_basic_project_data") as m:
        m.return_value = {"sandbox": {"mode": "inherit"}}
        assert (
            resolve_sandbox_mode({"sandbox_mode": "sandbox"}, "demo") == "sandbox"
        )


def test_resolve_project_override_wins():
    _import_projects_module()
    from hyperagent0.projects.resolve import resolve_sandbox_mode

    with patch("python.helpers.projects.load_basic_project_data") as m:
        m.return_value = {"sandbox": {"mode": "ssh"}}
        assert resolve_sandbox_mode({"sandbox_mode": "none"}, "demo") == "ssh"


def test_resolve_missing_sandbox_block_falls_back_to_global():
    _import_projects_module()
    from hyperagent0.projects.resolve import resolve_sandbox_mode

    with patch("python.helpers.projects.load_basic_project_data") as m:
        m.return_value = {}
        assert (
            resolve_sandbox_mode({"sandbox_mode": "sandbox"}, "demo") == "sandbox"
        )


def test_resolve_global_default_when_load_fails():
    _import_projects_module()
    from hyperagent0.projects.resolve import resolve_sandbox_mode

    with patch(
        "python.helpers.projects.load_basic_project_data", side_effect=Exception("nope")
    ):
        assert resolve_sandbox_mode({"sandbox_mode": "sandbox"}, "demo") == "sandbox"


def test_normalize_basic_data_seeds_default_sandbox_block():
    projects = _import_projects_module()
    out = projects._normalizeBasicData({})
    assert out.get("sandbox") == {"mode": "inherit"}


def test_normalize_basic_data_preserves_explicit_mode():
    projects = _import_projects_module()
    out = projects._normalizeBasicData({"sandbox": {"mode": "ssh"}})
    assert out.get("sandbox") == {"mode": "ssh"}


def test_set_agent_sandbox_mode_writes_to_additional():
    from hyperagent0.projects import (
        AGENT_CONFIG_KEY_SANDBOX_MODE,
        set_agent_sandbox_mode,
    )

    cfg = SimpleNamespace(additional={})
    set_agent_sandbox_mode(cfg, "sandbox")
    assert cfg.additional[AGENT_CONFIG_KEY_SANDBOX_MODE] == "sandbox"
