"""Smoke tests for the sandbox registry and CgroupBackend argv builder.

These tests do not spawn containers or systemd scopes — they only exercise
pure Python: registry lookups, schema defaults, and command construction.
"""

from __future__ import annotations

import shutil

import pytest

from hyperagent0.sandbox import (
    ProjectSandboxSettings,
    ResourceLimits,
    auto_detect_backend,
    list_registered_modes,
    recommend_mode_for_wizard,
)
from hyperagent0.sandbox.cgroup import CgroupBackend


def test_builtin_backends_registered() -> None:
    modes = list_registered_modes()
    assert "cgroup" in modes
    assert "docker" in modes
    assert "podman" in modes


def test_project_sandbox_settings_defaults() -> None:
    s = ProjectSandboxSettings()
    assert s.mode == "inherit"
    assert s.network == "internet"
    assert s.image is None
    assert s.persist_sandbox is False
    assert isinstance(s.resource_limits, ResourceLimits)


def test_project_sandbox_settings_broadened_literal_accepts_new_modes() -> None:
    # The Literal is enforced at type-check time, but we want a runtime
    # smoke check that the field accepts the new modes.
    for mode in ("inherit", "none", "sandbox", "ssh", "cgroup", "docker", "podman"):
        s = ProjectSandboxSettings(mode=mode)  # type: ignore[arg-type]
        assert s.mode == mode


def test_auto_detect_returns_a_known_mode() -> None:
    mode = auto_detect_backend()
    assert mode in {"sandbox", "cgroup", "docker", "podman", "none"}


def test_recommend_mode_for_wizard_returns_hint() -> None:
    mode, hint = recommend_mode_for_wizard()
    assert isinstance(mode, str)
    assert isinstance(hint, str)
    assert hint  # non-empty


def test_cgroup_argv_includes_systemd_run_and_unshare() -> None:
    settings = ProjectSandboxSettings(
        resource_limits=ResourceLimits(cpus=1.5, memory="2g"),
    )
    backend = CgroupBackend(project_dir="/tmp/proj", settings=settings)
    argv = backend.build_wrapper_argv(["/bin/bash"])
    assert argv[0] == "systemd-run"
    assert "--user" in argv
    assert "--scope" in argv
    assert "-p" in argv
    # Memory and CPU limits surface as `MemoryMax=` and `CPUQuota=`.
    flat = " ".join(argv)
    assert "MemoryMax=2g" in flat
    assert "CPUQuota=150%" in flat
    # unshare wraps the inner shell.
    assert "unshare" in argv
    assert "--mount" in argv
    assert "--map-root-user" in argv
    # Inner command at the end.
    assert argv[-1] == "/bin/bash"


def test_cgroup_argv_omits_limits_when_unset() -> None:
    backend = CgroupBackend(project_dir="/tmp/proj", settings=ProjectSandboxSettings())
    argv = backend.build_wrapper_argv(["/bin/sh"])
    flat = " ".join(argv)
    assert "MemoryMax" not in flat
    assert "CPUQuota" not in flat


def test_cgroup_argv_adds_net_namespace_when_network_none() -> None:
    settings = ProjectSandboxSettings(network="none")
    backend = CgroupBackend(project_dir="/tmp/proj", settings=settings)
    argv = backend.build_wrapper_argv(["/bin/sh"])
    assert "--net" in argv


@pytest.mark.skipif(
    shutil.which("systemd-run") is None or shutil.which("unshare") is None,
    reason="systemd-run / unshare not on PATH",
)
def test_cgroup_is_available_when_tools_present() -> None:
    # Don't assert True absolutely (cgroup v2 may be unmounted in some CI
    # environments) — just exercise the probe without crashing.
    result = CgroupBackend.is_available()
    assert isinstance(result, bool)
