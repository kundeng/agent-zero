"""Tests for the code_execution_tool sandbox routing (spec 01 task 1.5)."""

import importlib
from types import SimpleNamespace

import pytest


def _make_config(**overrides):
    return SimpleNamespace(
        code_exec_ssh_enabled=False,
        code_exec_ssh_addr="localhost",
        code_exec_ssh_port=22,
        code_exec_ssh_user="root",
        code_exec_ssh_pass="",
        additional={},
        **overrides,
    )


def _import_code_execution_tool():
    """Import or skip — module pulls models→litellm at top-level."""
    try:
        return importlib.import_module("python.tools.code_execution_tool")
    except ModuleNotFoundError as e:
        pytest.skip(f"code_execution_tool unavailable: {e}")


def test_set_and_get_agent_sandbox_mode_roundtrip():
    from hyperagent0.projects import (
        AGENT_CONFIG_KEY_SANDBOX_MODE,
        get_agent_sandbox_mode,
        set_agent_sandbox_mode,
    )

    cfg = _make_config()
    set_agent_sandbox_mode(cfg, "sandbox")
    assert cfg.additional[AGENT_CONFIG_KEY_SANDBOX_MODE] == "sandbox"
    assert get_agent_sandbox_mode(cfg) == "sandbox"


def test_resolve_sandbox_mode_no_project_uses_global():
    from hyperagent0.projects.resolve import resolve_sandbox_mode

    assert resolve_sandbox_mode({"sandbox_mode": "sandbox"}, None) == "sandbox"
    assert resolve_sandbox_mode({}, None) == "none"
    assert resolve_sandbox_mode({"sandbox_mode": ""}, None) == "none"


def test_resolver_prefers_explicit_sandbox_mode():
    cet = _import_code_execution_tool()
    from hyperagent0.projects import set_agent_sandbox_mode

    cfg = _make_config(code_exec_ssh_enabled=True)
    set_agent_sandbox_mode(cfg, "none")
    assert cet._resolve_sandbox_mode_with_legacy(cfg) == "none"


def test_resolver_auto_migrates_legacy_ssh():
    """code_exec_ssh_enabled=True with no explicit sandbox_mode → 'ssh'."""
    cet = _import_code_execution_tool()
    cet._legacy_ssh_warning_emitted = False
    cfg = _make_config(code_exec_ssh_enabled=True)
    assert cet._resolve_sandbox_mode_with_legacy(cfg) == "ssh"


def test_resolver_default_is_none():
    cet = _import_code_execution_tool()
    cfg = _make_config()
    assert cet._resolve_sandbox_mode_with_legacy(cfg) == "none"
