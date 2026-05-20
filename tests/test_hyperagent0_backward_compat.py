"""Backward-compatibility smoke tests (spec 01-host-first task 2.5).

Two guarantees:

1. ``DEPLOYMENT_MODE=docker`` keeps the legacy Docker deployment path
   (auto-detect via /.dockerenv also still works).
2. ``code_exec_ssh_enabled=True`` without an explicit ``sandbox_mode``
   auto-migrates to ``sandbox_mode='ssh'`` with a one-time deprecation
   warning. The on-disk legacy setting is untouched — migration happens at
   resolution time so users can roll back by clearing the flag.
"""

import importlib

import pytest


def test_deployment_mode_docker_via_env(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "docker")
    monkeypatch.setattr("sys.argv", ["prog"])
    monkeypatch.setattr("os.path.exists", lambda p: False)
    from hyperagent0.runtime import deployment_mode

    importlib.reload(deployment_mode)
    deployment_mode.resolve_deployment_mode.cache_clear()
    assert deployment_mode.is_docker_mode() is True
    assert deployment_mode.is_host_mode() is False


def test_deployment_mode_docker_via_marker(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    monkeypatch.setattr("sys.argv", ["prog"])
    monkeypatch.setattr("os.path.exists", lambda p: p == "/.dockerenv")
    from hyperagent0.runtime import deployment_mode

    importlib.reload(deployment_mode)
    deployment_mode.resolve_deployment_mode.cache_clear()
    assert deployment_mode.is_docker_mode() is True


def test_legacy_ssh_enabled_resolves_to_ssh_mode():
    cet = pytest.importorskip("python.tools.code_execution_tool")
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        code_exec_ssh_enabled=True,
        additional={},
    )
    cet._legacy_ssh_warning_emitted = False
    assert cet._resolve_sandbox_mode_with_legacy(cfg) == "ssh"


def test_explicit_sandbox_mode_overrides_legacy_flag():
    """Operators migrating off ssh can pin sandbox_mode='none' even when the
    stale code_exec_ssh_enabled flag is still True in their config."""
    cet = pytest.importorskip("python.tools.code_execution_tool")
    from types import SimpleNamespace

    from hyperagent0.projects import set_agent_sandbox_mode

    cfg = SimpleNamespace(code_exec_ssh_enabled=True, additional={})
    set_agent_sandbox_mode(cfg, "none")
    assert cet._resolve_sandbox_mode_with_legacy(cfg) == "none"


def test_deprecation_warning_fires_only_once(capsys):
    cet = pytest.importorskip("python.tools.code_execution_tool")
    from types import SimpleNamespace

    cfg = SimpleNamespace(code_exec_ssh_enabled=True, additional={})
    cet._legacy_ssh_warning_emitted = False

    cet._resolve_sandbox_mode_with_legacy(cfg)
    first = capsys.readouterr().out
    cet._resolve_sandbox_mode_with_legacy(cfg)
    second = capsys.readouterr().out

    assert "code_exec_ssh_enabled" in first
    assert "code_exec_ssh_enabled" not in second
