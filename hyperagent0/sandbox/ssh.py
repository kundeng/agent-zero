"""SshBackend — remote process via SSH (spec 01-host-first task 1.4).

Implements ``sandbox_mode='ssh'`` by wrapping upstream
:class:`python.helpers.shell_ssh.SSHInteractiveSession`. Connection
parameters come from the agent's runtime config (``code_exec_ssh_*``) — the
exact same fields the legacy ``code_exec_ssh_enabled=True`` path used.

Spec 01 task 1.5 hooks this into the code execution tool; the legacy flag
auto-migrates to ``sandbox_mode='ssh'`` with a deprecation warning.
"""

from __future__ import annotations

import importlib
from typing import Any

from .base import SandboxBackend


class SshBackend(SandboxBackend):
    mode = "ssh"

    def __init__(
        self,
        *,
        project_dir: str | None = None,
        connection: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(project_dir=project_dir)
        # Connection params are supplied by the caller (code_execution_tool)
        # from AgentConfig.code_exec_ssh_*. We keep the backend stateless
        # about defaults — the tool layer owns the resolved values.
        self.connection = connection or {}

    @classmethod
    def is_available(cls) -> bool:
        # Paramiko is part of upstream requirements.txt so this is a soft
        # check: we only confirm the module imports cleanly.
        try:
            importlib.import_module("paramiko")
        except Exception:
            return False
        return True

    @classmethod
    def install_hint(cls) -> str:
        return (
            "sandbox_mode=ssh requires paramiko (already in requirements.txt). "
            "If the import fails, run `pip install paramiko`."
        )

    async def open_shell(self, cwd: str | None = None) -> Any:
        from python.helpers.shell_ssh import SSHInteractiveSession

        conn = self.connection
        try:
            session = SSHInteractiveSession(
                conn["logger"],
                conn["hostname"],
                conn["port"],
                conn["username"],
                conn["password"],
                cwd=cwd,
            )
        except KeyError as e:
            raise ValueError(
                f"SshBackend.open_shell missing connection field: {e.args[0]!r}. "
                "Required keys: logger, hostname, port, username, password."
            ) from e
        await session.connect()
        return session
