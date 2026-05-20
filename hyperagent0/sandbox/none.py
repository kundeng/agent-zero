"""NoneBackend — local subprocess, no sandbox wrapper (spec 01 task 1.4)."""

from __future__ import annotations

from typing import Any

from .base import SandboxBackend


class NoneBackend(SandboxBackend):
    """Implements ``sandbox_mode='none'``.

    Returns a bare :class:`LocalInteractiveSession` — bit-for-bit identical
    to today's host-mode default. This is the safe fallback when no
    OS-level sandboxer is installed.
    """

    mode = "none"

    @classmethod
    def is_available(cls) -> bool:
        # Local PTY is always available; we rely on upstream's
        # tty_session helper to handle platform differences.
        return True

    async def open_shell(self, cwd: str | None = None) -> Any:
        # Lazy import: avoid pulling tty_session at module load.
        from python.helpers.shell_local import LocalInteractiveSession

        session = LocalInteractiveSession(cwd=cwd)
        await session.connect()
        return session
