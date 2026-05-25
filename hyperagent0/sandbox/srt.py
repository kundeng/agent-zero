"""SandboxBackendSrt — sandbox the whole bash session with srt.

Implements ``sandbox_mode='sandbox'`` using Anthropic's
`sandbox-runtime <https://github.com/anthropic-experimental/sandbox-runtime>`_
CLI (npm: ``@anthropic-ai/sandbox-runtime``).

Design (post spec 01 D8 + gap-fix pass)
---------------------------------------

The earlier P1 wrapper rewrote each individual command sent to the bash
session as ``srt --settings P -- <cmd>``. That meant every command ran in
a fresh srt child process; shell-state (``cd``, ``export``, aliases,
``set -x``, ``source ...``) died at the end of each command and never
reached the next one. The Claude Code docs confirm the canonical
pattern is "wrap the whole bash" so subprocess restrictions apply
uniformly and shell state persists — that is what this backend does
now.

How it works:

1. Resolve a profile path under ``~/.hyperagent0/sandbox/<project>.json``
   (or ``default.json`` when no project is active). The profile is
   regenerated on every ``open_shell`` so changes to ``project.json``
   or ``Settings.sandbox_network_default`` propagate without a daemon
   restart.

2. Build a session whose underlying PTY runs ``srt --settings <profile>
   -- /bin/bash`` instead of bare ``/bin/bash``. From the agent's point
   of view the shell behaves like a normal interactive bash; from the
   OS's point of view every process spawned inside that PTY is bounded
   by srt's namespace fence.

3. The interactive-session interface upstream consumes
   (``connect/send_command/read_output/close/full_output``) is
   preserved by subclassing :class:`LocalInteractiveSession`. The only
   override is :meth:`connect`, which constructs the TTY with the
   srt-wrapped command instead of the default shell.

Profile contents:

* ``fs.read.deny`` — always denies ``/etc/shadow``. Operator-level
  policy lives in srt's own config; we just guarantee that
  high-sensitivity files stay off.
* ``fs.read.allow`` — ``/`` by default (the agent often grep's docs
  outside the project tree).
* ``fs.write.allow`` — only the active project directory. ``cd /tmp``
  works because reads are permissive, but writes outside the project
  fail. Spec 09 P1.9 guarantees the caller always passes a real
  ``project_dir`` (resolved via ``_default`` for projectless chats);
  the legacy "empty allowlist" branch survives only as belt-and-suspenders
  for direct callers of the backend that bypass code_execution_tool.
* ``network.allow`` — merge of two sources:
  - ``Settings.sandbox_network_default`` (operator-set host-wide list)
  - ``project.json``'s ``network.allow`` (project-specific list)
  Union, deduplicated. Empty by default — projects opt in explicitly.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from .base import SandboxBackend


# Lazy module reference for ``python.helpers.tty_session``. Importing it
# at module load runs ``sys.stdin.reconfigure(errors="replace")`` which
# crashes under pytest (stdin is replaced with a ``DontReadFromInput``
# stub). Production code paths import it on demand via the property
# below; tests can monkeypatch this attribute to inject a fake.
tty_session: Any = None


def _load_tty_session() -> Any:
    """Import and cache ``python.helpers.tty_session`` on first call."""

    global tty_session
    if tty_session is None:
        from python.helpers import tty_session as _ts  # type: ignore

        tty_session = _ts
    return tty_session

logger = logging.getLogger(__name__)


_DEFAULT_PROFILE: dict[str, Any] = {
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
        # Use the project folder's basename so the profile filename is
        # stable across path layouts (e.g. ``/foo/bar/pirate`` →
        # ``pirate.json``). Falls back to ``default`` for the unbound
        # case.
        name = Path(project_dir).name or "default"
    return base / f"{name}.json"


def _read_project_network_allow(project_dir: str | None) -> list[str]:
    """Read ``network.allow`` from the project's ``project.json``.

    Returns ``[]`` if the project doesn't declare a network policy or
    the file is missing / malformed. We deliberately swallow errors
    here because the sandbox should always come up — a malformed
    project.json shouldn't crash code-exec, it should just fall back
    to the default-deny policy.
    """

    if not project_dir:
        return []
    pj = Path(project_dir) / ".a0proj" / "project.json"
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    net = data.get("network", {}) if isinstance(data, dict) else {}
    allow = net.get("allow", []) if isinstance(net, dict) else []
    return [str(host) for host in allow if isinstance(host, (str, bytes))]


def _global_network_default() -> list[str]:
    """Read ``Settings.sandbox_network_default`` (operator-wide allowlist).

    Lazy-imports the upstream settings module so this file remains
    importable from cold-start paths (e.g. ``haz status``) without
    paying for the settings load.
    """

    try:
        from python.helpers import settings as _settings  # type: ignore

        value = _settings.get_settings().get("sandbox_network_default", [])
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [str(host) for host in value if isinstance(host, (str, bytes))]


def _build_profile(project_dir: str | None) -> dict[str, Any]:
    """Compose the per-project srt profile.

    Deepcopy of the default so we never mutate the module-level dict
    (which would leak state between successive ``open_shell`` calls).
    """

    profile = copy.deepcopy(_DEFAULT_PROFILE)
    if project_dir:
        profile["fs"]["write"] = {"allow": [project_dir]}
    allow = sorted(set(_global_network_default()) | set(_read_project_network_allow(project_dir)))
    profile["network"] = {"allow": allow}
    return profile


def _ensure_profile(project_dir: str | None) -> Path:
    """Materialize the per-project srt profile on disk and return its path.

    Always rewrites so settings + project.json changes take effect
    immediately. If a future build needs hand-edit support, that's a
    separate "user-managed profile" mode — gate it on a config flag.
    """

    path = _profile_path(project_dir)
    profile = _build_profile(project_dir)
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return path


class _SrtSession:
    """Interactive session whose PTY runs ``srt --settings P -- /bin/bash``.

    Implements the duck-typed contract the code-execution tool consumes:

    * ``await connect()`` — start the PTY, drain the startup banner
    * ``await send_command(cmd)`` — write a line to the bash inside srt
    * ``await read_output(...)`` — pump stdout (raw + cleaned)
    * ``await close()`` — kill the PTY
    * attribute ``full_output: str`` — accumulator the tool reads

    Composition over inheritance: we don't subclass
    :class:`LocalInteractiveSession` because that class imports
    ``tty_session`` at module load (which performs ``sys.stdin.reconfigure``
    and breaks pytest). Holding a direct reference to a TTYSession
    sidesteps the inheritance chain.
    """

    def __init__(self, cwd: str | None, profile_path: Path) -> None:
        self.cwd = cwd
        self._profile_path = profile_path
        self._ts: Any = None
        self.full_output = ""

    async def connect(self) -> None:
        # TTYSession joins list-cmds with spaces, so shlex-quote anything
        # that might contain whitespace. ``srt`` and ``/bin/bash`` are
        # word-safe; the profile path may contain spaces (e.g. home dir
        # with spaces) so we quote it.
        cmd = " ".join(
            [
                "srt",
                "--settings",
                shlex.quote(str(self._profile_path)),
                "--",
                "/bin/bash",
            ]
        )
        ts_mod = _load_tty_session()
        self._ts = ts_mod.TTYSession(cmd, cwd=self.cwd)
        await self._ts.start()
        # Drain the srt+bash startup banner before the agent sends its
        # first command. Matches LocalInteractiveSession.connect.
        await self._ts.read_full_until_idle(idle_timeout=1, total_timeout=1)

    async def send_command(self, command: str) -> None:
        if self._ts is None:
            raise RuntimeError("srt session not connected")
        self.full_output = ""
        await self._ts.sendline(command)

    async def read_output(
        self, timeout: float = 0, reset_full_output: bool = False
    ) -> tuple[str, str | None]:
        if self._ts is None:
            raise RuntimeError("srt session not connected")
        if reset_full_output:
            self.full_output = ""
        partial = await self._ts.read_full_until_idle(
            idle_timeout=0.01, total_timeout=timeout
        )
        self.full_output += partial
        # Mirror upstream's clean_string behavior for consistency. Lazy
        # import for the same reason _load_tty_session is lazy.
        from python.helpers.shell_ssh import clean_string  # type: ignore

        clean_partial = clean_string(partial)
        clean_full = clean_string(self.full_output)
        if not clean_partial:
            return clean_full, None
        return clean_full, clean_partial

    async def close(self) -> None:
        if self._ts is None:
            return
        try:
            await self._ts.close()
        finally:
            self._ts = None


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
        profile = _ensure_profile(self.project_dir)
        session = _SrtSession(cwd=cwd, profile_path=profile)
        await session.connect()
        return session
