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


def test_legacy_ssh_enabled_resolves_to_ssh_mode(monkeypatch):
    try:
        cet = importlib.import_module("python.tools.code_execution_tool")
    except Exception as e:  # noqa: BLE001 — tty_session reconfigure under pytest
        pytest.skip(f"code_execution_tool unavailable: {e!r}")
    from types import SimpleNamespace

    # Force Settings.sandbox_mode='' so the legacy branch is the one
    # the resolver picks (otherwise an operator-set global would win).
    monkeypatch.setattr(
        "python.helpers.settings.get_settings",
        lambda: {"sandbox_mode": ""},
    )

    cfg = SimpleNamespace(
        code_exec_ssh_enabled=True,
        additional={},
    )
    cet._legacy_ssh_warning_emitted = False
    assert cet._resolve_sandbox_mode_with_legacy(cfg) == "ssh"


def test_explicit_sandbox_mode_overrides_legacy_flag(monkeypatch):
    """Operators migrating off ssh can pin Settings.sandbox_mode='none'
    even when the stale code_exec_ssh_enabled flag is still True.

    Spec 05 withdrawn 2026-05-22: previously this test used a per-agent
    override (``set_agent_sandbox_mode``). After withdrawal, the only
    knob is the global ``Settings.sandbox_mode``, which `Settings.reset`
    semantics let the operator override.
    """
    try:
        cet = importlib.import_module("python.tools.code_execution_tool")
    except Exception as e:  # noqa: BLE001 — tty_session reconfigure under pytest
        pytest.skip(f"code_execution_tool unavailable: {e!r}")
    from types import SimpleNamespace

    monkeypatch.setattr(
        "python.helpers.settings.get_settings",
        lambda: {"sandbox_mode": "none"},
    )

    cfg = SimpleNamespace(code_exec_ssh_enabled=True, additional={})
    assert cet._resolve_sandbox_mode_with_legacy(cfg) == "none"


def test_deprecation_warning_fires_only_once(capsys, monkeypatch):
    try:
        cet = importlib.import_module("python.tools.code_execution_tool")
    except Exception as e:  # noqa: BLE001 — tty_session reconfigure under pytest
        pytest.skip(f"code_execution_tool unavailable: {e!r}")
    from types import SimpleNamespace

    monkeypatch.setattr(
        "python.helpers.settings.get_settings",
        lambda: {"sandbox_mode": ""},
    )

    cfg = SimpleNamespace(code_exec_ssh_enabled=True, additional={})
    cet._legacy_ssh_warning_emitted = False

    cet._resolve_sandbox_mode_with_legacy(cfg)
    first = capsys.readouterr().out
    cet._resolve_sandbox_mode_with_legacy(cfg)
    second = capsys.readouterr().out

    assert "code_exec_ssh_enabled" in first
    assert "code_exec_ssh_enabled" not in second
