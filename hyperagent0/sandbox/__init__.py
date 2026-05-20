"""Sandbox backend registry (spec 01-host-first task 1.4, extended by spec 05).

Per spec D2/D9, the public surface is:

- :class:`SandboxBackend` — ABC. Each backend implements ``open_shell(cwd)``
  returning a connected interactive session (duck-typed against
  :class:`python.helpers.shell_local.LocalInteractiveSession` /
  :class:`python.helpers.shell_ssh.SSHInteractiveSession`), ``close()`` to
  release any backend-level resources, and the class-method
  ``is_available()`` reporting whether the backend can be used on this host.

- :func:`register_backend(mode, factory)` — for spec 05 (and future plugins)
  to register additional modes (``docker``, ``podman``, ``cgroup``) against
  the same ABC.

- :func:`get_backend(mode, *, project_dir=None, settings=None)` — instantiates
  the backend for the requested mode, raising a clear error with an install
  hint if the backend's dependency is missing.

Spec 01 ships three backends: ``none`` (no-op wrapper around
LocalInteractiveSession), ``sandbox`` (wraps Local with the ``srt`` CLI per
D8), and ``ssh`` (wraps SSHInteractiveSession). Spec 05 adds ``cgroup``,
``docker``, and ``podman``.

Backend modules are imported lazily by :func:`get_backend` so that merely
importing :mod:`hyperagent0.sandbox` does not pull in paramiko, docker SDK,
or other optional deps.

This module also exposes the spec-05 dataclasses (:class:`ResourceLimits`,
:class:`ProjectSandboxSettings`) used internally by backends. They are
*distinct* from the TypedDict of the same name in
:mod:`python.helpers.projects` (which is the JSON-on-disk representation in
``project.json``). At runtime, project config flows from JSON → TypedDict →
dataclass when constructed for a backend.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Union

from .base import SandboxBackend, SandboxUnavailableError

logger = logging.getLogger(__name__)


__all__ = [
    "SandboxBackend",
    "SandboxUnavailableError",
    "ResourceLimits",
    "ProjectSandboxSettings",
    "SandboxModeLiteral",
    "NetworkPolicy",
    "get_backend",
    "register_backend",
    "registered_modes",
    "auto_detect_backend",
    "recommend_mode_for_wizard",
]


# ---------------------------------------------------------------------------
# Registry — spec 01 authoritative version
# ---------------------------------------------------------------------------


# mode -> import path "module:attr" producing a SandboxBackend subclass.
# Kept as deferred references so importing the registry doesn't pull
# paramiko (ssh backend) or docker SDK (spec 05 docker backend).
_BUILTIN_BACKENDS: dict[str, str] = {
    # spec 01
    "none": "hyperagent0.sandbox.none:NoneBackend",
    "sandbox": "hyperagent0.sandbox.srt:SandboxBackendSrt",
    "ssh": "hyperagent0.sandbox.ssh:SshBackend",
    # spec 05
    "cgroup": "hyperagent0.sandbox.cgroup:CgroupBackend",
    "docker": "hyperagent0.sandbox.docker:DockerBackend",
    "podman": "hyperagent0.sandbox.podman:PodmanBackend",
}

# External registrations (future plugins) — stored as resolved classes.
_EXTRA_BACKENDS: dict[str, type[SandboxBackend]] = {}


def _resolve_builtin(mode: str) -> type[SandboxBackend]:
    target = _BUILTIN_BACKENDS[mode]
    module_path, attr = target.split(":", 1)
    import importlib

    module = importlib.import_module(module_path)
    klass = getattr(module, attr)
    if not isinstance(klass, type) or not issubclass(klass, SandboxBackend):
        raise TypeError(
            f"hyperagent0.sandbox: {target} is not a SandboxBackend subclass"
        )
    return klass


def register_backend(mode: str, backend_cls: type[SandboxBackend]) -> None:
    """Register an additional backend (for spec 05 and future extensions)."""
    if not isinstance(backend_cls, type) or not issubclass(backend_cls, SandboxBackend):
        raise TypeError("backend_cls must be a SandboxBackend subclass")
    _EXTRA_BACKENDS[mode] = backend_cls


def registered_modes() -> list[str]:
    """Return all known mode strings."""
    return sorted(set(_BUILTIN_BACKENDS) | set(_EXTRA_BACKENDS))


def get_backend(
    mode: str,
    *,
    project_dir: str | None = None,
    settings: Any = None,
) -> SandboxBackend:
    """Instantiate the backend for ``mode``.

    Raises:
        ValueError: if ``mode`` is not registered.
        SandboxUnavailableError: if the backend's dependency is missing.
    """
    if mode in _EXTRA_BACKENDS:
        backend_cls = _EXTRA_BACKENDS[mode]
    elif mode in _BUILTIN_BACKENDS:
        backend_cls = _resolve_builtin(mode)
    else:
        raise ValueError(
            f"hyperagent0.sandbox: unknown sandbox_mode={mode!r}. "
            f"Known modes: {', '.join(registered_modes())}"
        )

    if not backend_cls.is_available():
        raise SandboxUnavailableError(
            f"sandbox_mode={mode!r} requested but backend "
            f"{backend_cls.__name__} reports it is not available on this host. "
            f"{backend_cls.install_hint()}"
        )

    return backend_cls(project_dir=project_dir, settings=settings)


# ---------------------------------------------------------------------------
# Runtime config dataclasses (spec 05 — used internally by backends)
#
# These are distinct from the TypedDict in python/helpers/projects.py, which
# is the JSON-on-disk representation. Backends operate on these dataclasses;
# project config flows JSON → TypedDict → dataclass at backend construction.
# ---------------------------------------------------------------------------


SandboxModeLiteral = Literal[
    "inherit",
    "none",
    "sandbox",
    "ssh",
    "cgroup",
    "docker",
    "podman",
]


NetworkPolicy = Union[Literal["internet", "local-only", "none"], Dict[str, Any]]


@dataclass
class ResourceLimits:
    """Per-project resource limits enforced by the backend (best-effort).

    Backends that do not understand a field should ignore it without
    raising. Units:

    - ``cpus``: fractional CPU count (e.g. ``1.5`` for 1.5 cores).
    - ``memory``: byte string in docker/cgroup format (e.g. ``"2g"``).
    - ``timeout``: wall-clock seconds for a single shell session.
    - ``disk_quota``: byte string for ephemeral storage (e.g. ``"10g"``).
    """

    cpus: Optional[float] = None
    memory: Optional[str] = None
    timeout: Optional[int] = None
    disk_quota: Optional[str] = None


@dataclass
class ProjectSandboxSettings:
    """Per-project sandbox configuration (runtime dataclass form).

    Used by container/cgroup-spawning backends to read resource limits,
    network policy, and image overrides. The JSON-on-disk form is the
    TypedDict of the same name in :mod:`python.helpers.projects`.
    """

    mode: SandboxModeLiteral = "inherit"
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    network: NetworkPolicy = "internet"
    image: Optional[str] = None
    persist_sandbox: bool = False


# ---------------------------------------------------------------------------
# Auto-detection (spec-05 task 1.5)
# ---------------------------------------------------------------------------


def _probe_sandbox_srt() -> bool:
    return shutil.which("srt") is not None


def _probe_cgroup_v2() -> bool:
    if shutil.which("systemd-run") is None or shutil.which("unshare") is None:
        return False
    return os.path.isfile("/sys/fs/cgroup/cgroup.controllers")


def _probe_docker() -> bool:
    return shutil.which("docker") is not None


def _probe_podman() -> bool:
    return shutil.which("podman") is not None


# Probe order per spec-05 D3: sandbox (srt) → cgroup → docker → podman → none.
# Each probe must be cheap and side-effect-free (no daemon connection).
_PROBE_ORDER: List[Tuple[str, Callable[[], bool]]] = [
    ("sandbox", _probe_sandbox_srt),
    ("cgroup", _probe_cgroup_v2),
    ("docker", _probe_docker),
    ("podman", _probe_podman),
]


def auto_detect_backend() -> str:
    """Return the highest-priority available backend mode, or ``"none"``."""
    for mode, probe in _PROBE_ORDER:
        try:
            if probe():
                return mode
        except Exception:  # pragma: no cover — defensive
            logger.debug("probe for %s raised", mode, exc_info=True)
    return "none"


def recommend_mode_for_wizard() -> Tuple[str, str]:
    """Return ``(mode, hint)`` for the setup wizard.

    ``hint`` is a one-line human-readable explanation for the CLI/UI.
    """
    mode = auto_detect_backend()
    hints = {
        "sandbox": "srt (sandbox-runtime) detected — using fast userspace sandbox.",
        "cgroup": "cgroup v2 + systemd-run detected — using kernel-enforced resource limits.",
        "docker": "Docker detected — using fresh container per project.",
        "podman": "Podman detected — using rootless container per project.",
        "none": "No sandbox backend available — code will execute unsandboxed on the host.",
    }
    return mode, hints.get(mode, "")
