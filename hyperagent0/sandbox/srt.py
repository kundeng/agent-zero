"""SandboxBackendSrt — local subprocess wrapped with srt (spec 01 task 1.4, D8).

Implements ``sandbox_mode='sandbox'`` using Anthropic's
`sandbox-runtime <https://github.com/anthropic-experimental/sandbox-runtime>`_
CLI (npm: ``@anthropic-ai/sandbox-runtime``).

The wrapper is intentionally thin:

1. Probe ``srt`` on ``$PATH`` via :func:`shutil.which`. If absent,
   :meth:`is_available` returns False so the registry raises with an
   install hint.
2. Spawn a :class:`LocalInteractiveSession`, then intercept the next
   ``send_command`` so that the very next command — the actual user
   command — is re-wrapped as ``srt --settings <profile.json> <cmd>``.

A full integration would generate the per-project profile JSON (workspace
writable, system paths read-only, network policy from ``project.json``).
For spec 01 we write a minimal default profile under
``~/.hyperagent0/sandbox/<project>.json`` and surface its path via the
``--settings`` flag. The exact schema lives in srt's docs; we ship only the
defaults documented in the spec setup notes.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from .base import SandboxBackend


_DEFAULT_PROFILE: dict[str, Any] = {
    # Documented defaults from spec 01 D8:
    # - workspace dir writable
    # - system paths read-only
    # - network: allowlist (empty by default — projects opt in via project.json)
    "fs": {
        "read": {"deny": ["/etc/shadow"], "allow": ["/"]},
        "write": {"allow": []},
    },
    "network": {"allow": []},
}


def _state_dir() -> Path:
    return Path(os.environ.get("HYPERAGENT0_STATE_DIR", str(Path.home() / ".hyperagent0")))


def _profile_path(project_dir: str | None) -> Path:
    base = _state_dir() / "sandbox"
    base.mkdir(parents=True, exist_ok=True)
    name = "default"
    if project_dir:
        name = Path(project_dir).name or "default"
    return base / f"{name}.json"


def _ensure_profile(project_dir: str | None) -> Path:
    path = _profile_path(project_dir)
    if not path.exists():
        profile = dict(_DEFAULT_PROFILE)
        if project_dir:
            profile["fs"]["write"] = {"allow": [project_dir]}
        path.write_text(json.dumps(profile, indent=2))
    return path


class SandboxBackendSrt(SandboxBackend):
    mode = "sandbox"

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("srt") is not None

    @classmethod
    def install_hint(cls) -> str:
        return (
            "Install Anthropic sandbox-runtime: "
            "`npm install -g @anthropic-ai/sandbox-runtime`. "
            "Linux additionally requires system packages: bubblewrap, socat, ripgrep."
        )

    async def open_shell(self, cwd: str | None = None) -> Any:
        from python.helpers.shell_local import LocalInteractiveSession

        profile = _ensure_profile(self.project_dir)
        session = LocalInteractiveSession(cwd=cwd)
        await session.connect()

        # Wrap send_command so each user command is invoked under `srt`.
        # We can't run the bash session itself under srt without breaking
        # the PTY semantics upstream relies on; wrapping per-command is the
        # least-invasive integration point.
        original_send = session.send_command
        profile_arg = shlex.quote(str(profile))

        async def _wrapped(command: str) -> None:
            # Pass-through for empty/whitespace (just press-Enter) commands.
            if not command.strip():
                await original_send(command)
                return
            wrapped = f"srt --settings {profile_arg} -- {command}"
            await original_send(wrapped)

        session.send_command = _wrapped  # type: ignore[method-assign]
        return session
