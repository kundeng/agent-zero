"""SandboxBackend ABC (spec 01-host-first task 1.4)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SandboxUnavailableError(RuntimeError):
    """Raised when a requested sandbox backend's dependency is missing."""


class SandboxBackend(ABC):
    """Abstract base class for sandbox backends.

    The session returned by :meth:`open_shell` must duck-type against the
    upstream interactive-session interface used by
    :mod:`python.tools.code_execution_tool`:

    - ``await session.connect()``
    - ``await session.send_command(cmd)``
    - ``await session.read_output(timeout=..., reset_full_output=...)``
    - ``await session.close()``
    - attribute ``full_output: str``

    Today that means returning either :class:`LocalInteractiveSession` or
    :class:`SSHInteractiveSession` (possibly with the command pre-wrapped, as
    the ``sandbox`` mode does with ``srt``).
    """

    #: Mode string this backend implements. Subclasses override.
    mode: str = ""

    def __init__(
        self,
        *,
        project_dir: str | None = None,
        settings: Any = None,
    ) -> None:
        self.project_dir = project_dir
        self.settings = settings

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Return True if this backend can be used on the current host."""

    @classmethod
    def install_hint(cls) -> str:
        """Human-readable hint for resolving an unavailable backend."""
        return ""

    @abstractmethod
    async def open_shell(self, cwd: str | None = None) -> Any:
        """Open and return a connected interactive session."""

    async def close(self) -> None:
        """Release any backend-level resources. No-op by default."""
        return None
