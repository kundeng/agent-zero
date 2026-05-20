"""hyperagent0.sandbox — SandboxBackend ABC, registry, schema, and built-in backends.

This package extends the spec-01 sandbox infrastructure with the
isolated-process-tree backends (cgroup, docker, podman) per spec-05.

NOTE (merge reconciliation): Spec 01 is the canonical owner of the
``SandboxBackend`` ABC, the registry (``register_backend`` / ``get_backend``),
``ProjectSandboxSettings``, and the ``none`` / ``sandbox`` / ``ssh`` backends.
This worktree was authored against a stub of that contract because spec 01
landed in a parallel worktree. At merge time, the stubs in this file
(``SandboxBackend``, ``register_backend``, ``get_backend``, ``ResourceLimits``,
``ProjectSandboxSettings``) should be reconciled with spec 01's authoritative
versions. The spec-05 additions to preserve are:

- ``ProjectSandboxSettings.mode`` literal includes ``cgroup``/``docker``/``podman``.
- Added fields: ``resource_limits``, ``network``, ``image``, ``persist_sandbox``.
- ``register_backend`` calls in :mod:`hyperagent0.sandbox.cgroup`,
  :mod:`hyperagent0.sandbox.docker`, :mod:`hyperagent0.sandbox.podman`.
- :func:`auto_detect_backend` and :func:`recommend_mode_for_wizard`.
"""

from __future__ import annotations

import logging
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Type,
    Union,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ABC + registry (STUB — owned by spec 01; replace at merge time)
# ---------------------------------------------------------------------------


class SandboxBackend(ABC):
    """Abstract base class for sandbox backends.

    Stub for spec-01's ABC. The real ABC will live in spec 01's
    ``hyperagent0/sandbox/__init__.py``; this stub exists so spec-05's
    backends compile against a stable contract.
    """

    #: Human-readable mode name, matches ``ProjectSandboxSettings.mode``.
    mode: str = "abstract"

    def __init__(self, project_dir: Optional[str] = None, settings: Optional["ProjectSandboxSettings"] = None) -> None:
        self.project_dir = project_dir
        self.settings = settings

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Probe whether this backend's dependencies are present on this host."""

    @abstractmethod
    async def open_shell(self, cwd: str) -> Any:
        """Open an interactive shell session rooted at ``cwd``.

        Returns an object with the same shape as
        :class:`python.helpers.shell_local.LocalInteractiveSession`
        (``connect``, ``close``, ``send_command``, ``read_output``).
        """

    @abstractmethod
    async def close(self) -> None:
        """Release any backend resources (containers, cgroups, sessions)."""


# Registry — module-level dict keyed by mode name.
_BACKENDS: Dict[str, Type[SandboxBackend]] = {}


def register_backend(mode_name: str, backend_class: Type[SandboxBackend]) -> None:
    """Register a backend class under ``mode_name``.

    Re-registration is a no-op with a warning (so test reloads don't blow up).
    """
    if mode_name in _BACKENDS and _BACKENDS[mode_name] is not backend_class:
        logger.warning(
            "sandbox backend %r already registered to %r; overriding with %r",
            mode_name,
            _BACKENDS[mode_name].__name__,
            backend_class.__name__,
        )
    _BACKENDS[mode_name] = backend_class


def get_backend(mode: str, project_dir: Optional[str] = None, settings: Optional["ProjectSandboxSettings"] = None) -> SandboxBackend:
    """Construct a backend instance for ``mode``.

    Raises:
        KeyError: if ``mode`` is not registered.
        RuntimeError: if the backend's dependency is missing
            (the backend raises a friendly install hint).
    """
    if mode not in _BACKENDS:
        raise KeyError(
            f"sandbox mode {mode!r} not registered; available: {sorted(_BACKENDS)}"
        )
    cls = _BACKENDS[mode]
    return cls(project_dir=project_dir, settings=settings)


def list_registered_modes() -> List[str]:
    return sorted(_BACKENDS)


# ---------------------------------------------------------------------------
# Schema (STUB — owned by spec 01; spec 05 broadens it)
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
    """Per-project sandbox configuration.

    Stub for spec-01's schema. Spec 05 broadens the ``mode`` literal and adds
    ``resource_limits``, ``network``, ``image``, ``persist_sandbox``. Keep
    these fields when reconciling at merge time.
    """

    mode: SandboxModeLiteral = "inherit"
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    network: NetworkPolicy = "internet"
    image: Optional[str] = None
    persist_sandbox: bool = False


# ---------------------------------------------------------------------------
# Auto-detection (spec-05 task 1.5)
# ---------------------------------------------------------------------------


# Probe order per spec-05 task 1.5: sandbox (srt) → cgroup → docker → podman → none.
# Each entry is (mode_name, probe_callable). The probe must be cheap and
# side-effect-free (no daemon connection, just `which` and filesystem checks).
_PROBE_ORDER: List[Tuple[str, Callable[[], bool]]] = []


def _probe_sandbox_srt() -> bool:
    return shutil.which("srt") is not None


def _probe_cgroup_v2() -> bool:
    if shutil.which("systemd-run") is None or shutil.which("unshare") is None:
        return False
    # cgroup v2 is mounted at /sys/fs/cgroup with a cgroup.controllers file.
    return os.path.isfile("/sys/fs/cgroup/cgroup.controllers")


def _probe_docker() -> bool:
    return shutil.which("docker") is not None


def _probe_podman() -> bool:
    return shutil.which("podman") is not None


_PROBE_ORDER.extend(
    [
        ("sandbox", _probe_sandbox_srt),
        ("cgroup", _probe_cgroup_v2),
        ("docker", _probe_docker),
        ("podman", _probe_podman),
    ]
)


def auto_detect_backend() -> str:
    """Return the highest-priority available backend mode, or ``"none"``.

    Probe order matches spec-05 D3: ``sandbox → cgroup → docker → podman → none``.
    Does not attempt to connect to any daemon — only filesystem-level checks.
    """
    for mode, probe in _PROBE_ORDER:
        try:
            if probe():
                return mode
        except Exception:  # pragma: no cover — defensive
            logger.debug("probe for %s raised", mode, exc_info=True)
    return "none"


def recommend_mode_for_wizard() -> Tuple[str, str]:
    """Return ``(mode, hint)`` for the setup wizard.

    ``hint`` is a one-line human-readable explanation suitable for display
    in the CLI or web UI.
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


# ---------------------------------------------------------------------------
# Register spec-05 backends on import. Each module guards against missing
# heavy deps (lazy imports inside __init__); registration is cheap.
# ---------------------------------------------------------------------------


def _register_builtin_backends() -> None:
    # Import locally to avoid import cycles and keep startup lean.
    from hyperagent0.sandbox import cgroup as _cgroup  # noqa: F401
    from hyperagent0.sandbox import docker as _docker  # noqa: F401
    from hyperagent0.sandbox import podman as _podman  # noqa: F401


try:
    _register_builtin_backends()
except Exception:  # pragma: no cover — defensive
    logger.exception("failed to register built-in sandbox backends")


__all__ = [
    "SandboxBackend",
    "register_backend",
    "get_backend",
    "list_registered_modes",
    "ProjectSandboxSettings",
    "ResourceLimits",
    "SandboxModeLiteral",
    "NetworkPolicy",
    "auto_detect_backend",
    "recommend_mode_for_wizard",
]
