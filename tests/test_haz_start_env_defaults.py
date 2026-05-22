"""Tests for ``haz start``'s env-var → settings.json bridge.

Spec 07 P2.1: compose users set CHAT_MODEL_* / SANDBOX_MODE in .env,
the container reads them on startup and persists them — but only for
fields not already set, so a UI-configured value survives restarts.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Redirect paths.settings_path so each test gets a fresh file."""

    target = tmp_path / "usr" / "settings.json"
    # Import deferred so we patch the module the function actually uses.
    from hyperagent0 import paths as _paths

    monkeypatch.setattr(_paths, "settings_path", lambda: target)
    return target


@pytest.fixture
def clear_env(monkeypatch):
    """Strip the env vars we care about so tests don't bleed."""

    for name in (
        "CHAT_MODEL_PROVIDER",
        "CHAT_MODEL_NAME",
        "CHAT_MODEL_API_BASE",
        "SANDBOX_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


def _apply(monkeypatch_env: dict[str, str] | None = None):
    """Helper: import start.py and call the env applier."""

    # Re-import each call so module-level state is fresh.
    from hyperagent0.cli_commands import start as start_mod
    importlib.reload(start_mod)
    return start_mod._apply_env_defaults_to_settings


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def test_no_env_vars_no_file_written(isolated_settings, clear_env):
    _apply()()
    assert not isolated_settings.exists()


def test_sandbox_mode_env_writes_settings(isolated_settings, clear_env, monkeypatch):
    monkeypatch.setenv("SANDBOX_MODE", "docker")
    _apply()()
    assert _read(isolated_settings) == {"sandbox_mode": "docker"}


def test_multiple_env_vars_combine(isolated_settings, clear_env, monkeypatch):
    monkeypatch.setenv("CHAT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("CHAT_MODEL_NAME", "cc/claude-sonnet-4-6")
    monkeypatch.setenv("CHAT_MODEL_API_BASE", "http://proxy:20128")
    monkeypatch.setenv("SANDBOX_MODE", "podman")
    _apply()()
    assert _read(isolated_settings) == {
        "chat_model_provider": "openai",
        "chat_model_name": "cc/claude-sonnet-4-6",
        "chat_model_api_base": "http://proxy:20128",
        "sandbox_mode": "podman",
    }


def test_existing_settings_not_overwritten(isolated_settings, clear_env, monkeypatch):
    """UI-configured value must survive a restart with conflicting env."""

    isolated_settings.parent.mkdir(parents=True, exist_ok=True)
    isolated_settings.write_text(
        json.dumps(
            {
                "chat_model_provider": "anthropic",
                "chat_model_name": "user-picked-via-ui",
            }
        )
    )
    monkeypatch.setenv("CHAT_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("CHAT_MODEL_NAME", "stale-default-from-env")
    monkeypatch.setenv("SANDBOX_MODE", "docker")

    _apply()()

    data = _read(isolated_settings)
    # UI values preserved.
    assert data["chat_model_provider"] == "anthropic"
    assert data["chat_model_name"] == "user-picked-via-ui"
    # New field (sandbox_mode) gets the env default.
    assert data["sandbox_mode"] == "docker"


def test_empty_env_var_does_not_set_key(isolated_settings, clear_env, monkeypatch):
    """Compose .env often has CHAT_MODEL_PROVIDER= (blank). Treat as unset."""

    monkeypatch.setenv("CHAT_MODEL_PROVIDER", "")
    monkeypatch.setenv("SANDBOX_MODE", "docker")
    _apply()()
    data = _read(isolated_settings)
    assert "chat_model_provider" not in data
    assert data.get("sandbox_mode") == "docker"


def test_empty_existing_value_gets_replaced(isolated_settings, clear_env, monkeypatch):
    """A "" placeholder in settings.json (e.g., from a wizard skip) should
    let the env var win — empty isn't a real value."""

    isolated_settings.parent.mkdir(parents=True, exist_ok=True)
    isolated_settings.write_text(json.dumps({"sandbox_mode": ""}))
    monkeypatch.setenv("SANDBOX_MODE", "docker")
    _apply()()
    assert _read(isolated_settings)["sandbox_mode"] == "docker"
