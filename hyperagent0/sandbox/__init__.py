"""Sandbox backend registry (spec 01-host-first task 1.4).

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

- :func:`get_backend(mode, *, project_dir=None)` — instantiates the backend
  for the requested mode, raising a clear error with an install hint if the
  backend's dependency is missing.

Spec 01 ships three backends: ``none`` (no-op wrapper around
LocalInteractiveSession), ``sandbox`` (wraps Local with the ``srt`` CLI per
D8), and ``ssh`` (wraps SSHInteractiveSession).

Backend modules are imported lazily by :func:`get_backend` so that merely
importing :mod:`hyperagent0.sandbox` does not pull in paramiko or other
optional deps.
"""

from __future__ import annotations

from .base import SandboxBackend, SandboxUnavailableError

__all__ = [
    "SandboxBackend",
    "SandboxUnavailableError",
    "get_backend",
    "register_backend",
    "registered_modes",
]


# mode -> import path "module:attr" producing a SandboxBackend subclass.
# Kept as deferred references so importing the registry doesn't pull
# paramiko (ssh backend) or anything else heavy.
_BUILTIN_BACKENDS: dict[str, str] = {
    "none": "hyperagent0.sandbox.none:NoneBackend",
    "sandbox": "hyperagent0.sandbox.srt:SandboxBackendSrt",
    "ssh": "hyperagent0.sandbox.ssh:SshBackend",
}

# External registrations (spec 05 etc.) — stored as resolved classes.
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
    return sorted({*_BUILTIN_BACKENDS.keys(), *_EXTRA_BACKENDS.keys()})


def get_backend(mode: str, *, project_dir: str | None = None) -> SandboxBackend:
    """Return an instantiated backend for ``mode``.

    Raises :class:`SandboxUnavailableError` with an install hint if the
    backend's dependency is missing.
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

    return backend_cls(project_dir=project_dir)
