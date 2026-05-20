"""Tests for hyperagent0.runtime.deployment_mode (spec 01 task 1.2)."""

import importlib
import os

import pytest


def _reload_module():
    from hyperagent0.runtime import deployment_mode

    importlib.reload(deployment_mode)
    deployment_mode.resolve_deployment_mode.cache_clear()
    return deployment_mode


def test_env_var_host(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "host")
    monkeypatch.setattr("sys.argv", ["prog"])
    monkeypatch.setattr("os.path.exists", lambda p: False)
    mod = _reload_module()
    assert mod.is_host_mode() is True
    assert mod.is_docker_mode() is False


def test_env_var_docker(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "docker")
    monkeypatch.setattr("sys.argv", ["prog"])
    monkeypatch.setattr("os.path.exists", lambda p: False)
    mod = _reload_module()
    assert mod.is_docker_mode() is True
    assert mod.is_host_mode() is False


def test_cli_flag_dockerized(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    monkeypatch.setattr("sys.argv", ["prog", "--dockerized"])
    monkeypatch.setattr("os.path.exists", lambda p: False)
    mod = _reload_module()
    assert mod.is_docker_mode() is True


def test_dockerenv_marker(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    monkeypatch.setattr("sys.argv", ["prog"])
    monkeypatch.setattr("os.path.exists", lambda p: p == "/.dockerenv")
    mod = _reload_module()
    assert mod.is_docker_mode() is True


def test_default_is_host(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    monkeypatch.setattr("sys.argv", ["prog"])
    monkeypatch.setattr("os.path.exists", lambda p: False)
    mod = _reload_module()
    assert mod.is_host_mode() is True


def test_env_overrides_dockerenv(monkeypatch):
    """Explicit env var beats /.dockerenv presence."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "host")
    monkeypatch.setattr("sys.argv", ["prog"])
    monkeypatch.setattr("os.path.exists", lambda p: p == "/.dockerenv")
    mod = _reload_module()
    assert mod.is_host_mode() is True
