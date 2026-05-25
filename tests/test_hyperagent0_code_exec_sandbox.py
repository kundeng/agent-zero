"""Tests for code_execution_tool sandbox-mode resolution (spec 01 task 1.5).

Originally this file also covered per-project sandbox overrides from
spec 05 (``hyperagent0.projects.resolve`` + ``set_agent_sandbox_mode``).
Spec 05 was withdrawn 2026-05-22 — sandbox_mode is now a single global
``Settings.sandbox_mode``, no per-agent override. The dead tests were
deleted 2026-05-25 as part of orphan-test cleanup.

What remains: the legacy-flag migration path
(``code_exec_ssh_enabled=True`` → ``sandbox_mode='ssh'``) and the
defaults.
"""

from __future__ import annotations

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
    """Import or skip — the upstream module pulls
    ``python.helpers.tty_session`` at top-level, which calls
    ``sys.stdin.reconfigure(errors='replace')``. Under pytest, stdin is
    replaced with a ``DontReadFromInput`` stub that has no
    ``reconfigure`` attribute, so the import raises ``AttributeError``.
    Catch every load-time failure and skip cleanly — the resolver logic
    these tests cover is best exercised at integration time.
    """
    try:
        return importlib.import_module("python.tools.code_execution_tool")
    except Exception as e:  # noqa: BLE001 - any module-load error → skip
        pytest.skip(f"code_execution_tool unavailable: {e!r}")


def test_resolver_default_is_none():
    """No global sandbox_mode and no legacy flag → ``'none'``."""

    cet = _import_code_execution_tool()
    cfg = _make_config()
    assert cet._resolve_sandbox_mode_with_legacy(cfg) == "none"


def test_resolver_auto_migrates_legacy_ssh(monkeypatch):
    """``code_exec_ssh_enabled=True`` with no explicit sandbox_mode → ``'ssh'``."""

    cet = _import_code_execution_tool()
    cet._legacy_ssh_warning_emitted = False

    # Force Settings.sandbox_mode='' so the legacy branch triggers.
    monkeypatch.setattr(
        "python.helpers.settings.get_settings",
        lambda: {"sandbox_mode": ""},
    )

    cfg = _make_config(code_exec_ssh_enabled=True)
    assert cet._resolve_sandbox_mode_with_legacy(cfg) == "ssh"


def test_global_sandbox_mode_wins_over_legacy_flag(monkeypatch):
    """Operators migrating off ssh can pin ``Settings.sandbox_mode='none'``
    even when the stale ``code_exec_ssh_enabled`` flag is still True.
    """

    cet = _import_code_execution_tool()
    monkeypatch.setattr(
        "python.helpers.settings.get_settings",
        lambda: {"sandbox_mode": "none"},
    )

    cfg = _make_config(code_exec_ssh_enabled=True)
    assert cet._resolve_sandbox_mode_with_legacy(cfg) == "none"
