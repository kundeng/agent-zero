"""Sandbox backend registry (spec 01-host-first task 1.4).

Public surface:

- :class:`SandboxBackend` — ABC. Each backend implements ``open_shell(cwd)``
  returning a connected interactive session (duck-typed against
  :class:`python.helpers.shell_local.LocalInteractiveSession` /
  :class:`python.helpers.shell_ssh.SSHInteractiveSession`), ``close()`` to
  release any backend-level resources, and the class-method
  ``is_available()`` reporting whether the backend can be used on this host.
- :func:`register_backend` / :func:`get_backend` — the public registry.

Three backends ship: ``none`` (no-op wrapper around LocalInteractiveSession),
``sandbox`` (wraps Local with the ``srt`` CLI per spec 01 D8), and ``ssh``
(wraps SSHInteractiveSession).

Backend modules are imported lazily by :func:`get_backend` so that merely
importing :mod:`hyperagent0.sandbox` does not pull in paramiko or other
optional deps.
"""

from __future__ import annotations

import logging
import shutil
from typing import Any, Callable, List, Literal, Tuple

from .base import SandboxBackend, SandboxUnavailableError

logger = logging.getLogger(__name__)


__all__ = [
    "SandboxBackend",
    "SandboxUnavailableError",
    "SandboxModeLiteral",
    "get_backend",
    "register_backend",
    "registered_modes",
    "list_registered_modes",
    "auto_detect_backend",
    "recommend_mode_for_wizard",
]


# ---------------------------------------------------------------------------
# Registry — spec 01 authoritative version
# ---------------------------------------------------------------------------


# mode -> import path "module:attr" producing a SandboxBackend subclass.
# Kept as deferred references so importing the registry doesn't pull
# paramiko (ssh backend) or other optional deps.
_BUILTIN_BACKENDS: dict[str, str] = {
    "none": "hyperagent0.sandbox.none:NoneBackend",
    "sandbox": "hyperagent0.sandbox.srt:SandboxBackendSrt",
    "ssh": "hyperagent0.sandbox.ssh:SshBackend",
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
    """Register an additional backend (for future extensions / plugins)."""
    if not isinstance(backend_cls, type) or not issubclass(backend_cls, SandboxBackend):
        raise TypeError("backend_cls must be a SandboxBackend subclass")
    _EXTRA_BACKENDS[mode] = backend_cls


def registered_modes() -> list[str]:
    """Return all known mode strings."""
    return sorted(set(_BUILTIN_BACKENDS) | set(_EXTRA_BACKENDS))


# Spec-05 backwards-compat alias.
list_registered_modes = registered_modes


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


SandboxModeLiteral = Literal["none", "sandbox", "ssh"]


def _probe_sandbox_srt() -> bool:
    return shutil.which("srt") is not None


_PROBE_ORDER: List[Tuple[str, Callable[[], bool]]] = [
    ("sandbox", _probe_sandbox_srt),
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
    """Return ``(mode, hint)`` for the setup wizard."""
    mode = auto_detect_backend()
    hints = {
        "sandbox": "srt (sandbox-runtime) detected — using fast userspace sandbox.",
        "none": "No sandbox backend available — code will execute unsandboxed on the host.",
    }
    return mode, hints.get(mode, "")
