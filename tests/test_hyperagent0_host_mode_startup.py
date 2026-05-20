"""Host-mode startup smoke test (spec 01-host-first task 2.2).

The agent must come up cleanly without a Docker daemon on the host. We
exercise the runtime helpers and the sandbox registry without spinning up
Flask or LiteLLM — those layers are independent of the deployment-mode
decision.
"""

import importlib
import os
import shutil

import pytest


def test_host_mode_when_docker_binary_absent(monkeypatch):
    """If `docker` is not on PATH and /.dockerenv is missing, default to host."""
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    monkeypatch.setattr("sys.argv", ["prog"])
    monkeypatch.setattr("os.path.exists", lambda p: False)

    # Wipe docker binary from PATH for this process.
    original_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "docker" else original_which(name))

    from hyperagent0.runtime import deployment_mode

    importlib.reload(deployment_mode)
    deployment_mode.resolve_deployment_mode.cache_clear()
    assert deployment_mode.is_host_mode() is True
    assert deployment_mode.is_docker_mode() is False


def test_sandbox_registry_works_without_docker(monkeypatch):
    """The sandbox registry must initialize without docker; only 'none' is needed."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    from hyperagent0.sandbox import get_backend, registered_modes

    assert "none" in registered_modes()
    backend = get_backend("none")
    assert backend.mode == "none"


def _import_and_reload_runtime():
    # When other tests cache a partial python.helpers, ``from python.helpers
    # import runtime`` can succeed yielding the cached module, but a subsequent
    # importlib.reload() re-triggers transitive imports (settings.py →
    # browser_use_monkeypatch → browser_use) that may not be installed. Wrap
    # both the import and the reload in the same skip envelope.
    try:
        from python.helpers import runtime as rt
        importlib.reload(rt)
    except ModuleNotFoundError as e:
        pytest.skip(f"python.helpers.runtime unavailable: {e}")
    return rt


def test_is_development_default_false(monkeypatch):
    """Host-mode users must not silently land in development mode."""
    monkeypatch.delenv("A0_DEV", raising=False)
    monkeypatch.setattr("sys.argv", ["prog"])
    rt = _import_and_reload_runtime()
    rt.initialize()
    assert rt.is_development() is False


def test_is_development_opts_in_via_env(monkeypatch):
    monkeypatch.setenv("A0_DEV", "1")
    monkeypatch.setattr("sys.argv", ["prog"])
    rt = _import_and_reload_runtime()
    rt.initialize()
    assert rt.is_development() is True
