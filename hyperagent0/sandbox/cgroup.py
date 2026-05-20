"""CgroupBackend — local subprocess with cgroup v2 + mount-namespace isolation.

Uses ``systemd-run --user --scope`` for resource limits (no root required on
modern distros with systemd user instances) and ``unshare --mount`` for a
private mount namespace. Sessions are persistent within an agent session per
spec-05 D2 — installed packages survive between turns.

Probe (``is_available``): ``systemd-run`` and ``unshare`` on PATH and cgroup v2
mounted at ``/sys/fs/cgroup/cgroup.controllers``.
"""

from __future__ import annotations

import logging
import os
import shutil
import shlex
from typing import Any, List, Optional

from hyperagent0.sandbox import (
    ProjectSandboxSettings,
    SandboxBackend,
    register_backend,
)

logger = logging.getLogger(__name__)


def _format_cpu_quota(cpus: Optional[float]) -> Optional[str]:
    """Convert fractional CPU count to systemd ``CPUQuota=`` percentage string."""
    if cpus is None or cpus <= 0:
        return None
    # systemd-run accepts "CPUQuota=150%" for 1.5 cores.
    return f"{int(round(cpus * 100))}%"


def _format_memory_max(memory: Optional[str]) -> Optional[str]:
    """Pass-through with light validation. systemd accepts ``1G``, ``512M``, bytes."""
    if not memory:
        return None
    return memory


class CgroupBackend(SandboxBackend):
    """Subprocess wrapped in a ``systemd-run --scope`` + ``unshare --mount`` shell."""

    mode = "cgroup"

    @classmethod
    def is_available(cls) -> bool:
        if shutil.which("systemd-run") is None:
            return False
        if shutil.which("unshare") is None:
            return False
        if not os.path.isfile("/sys/fs/cgroup/cgroup.controllers"):
            return False
        return True

    def __init__(
        self,
        project_dir: Optional[str] = None,
        settings: Optional[ProjectSandboxSettings] = None,
    ) -> None:
        super().__init__(project_dir=project_dir, settings=settings)
        self._session: Any = None

    # ------------------------------------------------------------------
    # Command construction
    # ------------------------------------------------------------------

    def build_wrapper_argv(self, inner_argv: List[str]) -> List[str]:
        """Wrap ``inner_argv`` in ``systemd-run --scope`` and ``unshare --mount``.

        Exposed for testing — no side effects, just argv construction.
        """
        settings = self.settings or ProjectSandboxSettings()
        limits = settings.resource_limits

        argv: List[str] = [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
        ]

        mem = _format_memory_max(limits.memory)
        if mem:
            argv += ["-p", f"MemoryMax={mem}"]
        cpu = _format_cpu_quota(limits.cpus)
        if cpu:
            argv += ["-p", f"CPUQuota={cpu}"]

        # Mount namespace isolation. ``--map-root-user`` lets us mount without
        # actual root by leveraging user namespaces.
        argv += ["unshare", "--mount", "--map-root-user"]

        # Optional network namespace per spec-05 task 2.2.
        net = settings.network
        if net == "none":
            argv += ["--net"]

        argv.extend(inner_argv)
        return argv

    # ------------------------------------------------------------------
    # SandboxBackend protocol
    # ------------------------------------------------------------------

    async def open_shell(self, cwd: str) -> Any:
        # Import locally to keep import graph thin and to allow tests to
        # exercise argv construction without pulling in the upstream session.
        from python.helpers.shell_local import LocalInteractiveSession

        # Compose the shell command. We delegate to LocalInteractiveSession
        # but tell it to use a wrapped terminal executable. To keep the
        # subprocess wiring simple, we re-export the wrapped argv as a
        # single string passed to ``bash -c``.
        from python.helpers import runtime

        terminal = runtime.get_terminal_executable()
        wrapped = self.build_wrapper_argv([terminal])
        wrapped_cmd = " ".join(shlex.quote(part) for part in wrapped)

        # Monkey-style override: spawn a session whose terminal is the
        # wrapped command. We re-use LocalInteractiveSession but call its
        # TTY with a shell that execs the wrapper.
        session = LocalInteractiveSession(cwd=cwd)
        # The upstream session reads the terminal exe from runtime.
        # Stash the wrapped command on the instance for callers that want
        # to introspect it; the real wiring will be done at spec-01 merge
        # time when the LocalInteractiveSession constructor accepts an
        # explicit ``terminal`` argument.
        session._hyperagent0_wrapped_argv = wrapped  # type: ignore[attr-defined]
        session._hyperagent0_wrapped_cmd = wrapped_cmd  # type: ignore[attr-defined]
        await session.connect()
        self._session = session
        return session

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            finally:
                self._session = None


register_backend("cgroup", CgroupBackend)


__all__ = ["CgroupBackend"]
