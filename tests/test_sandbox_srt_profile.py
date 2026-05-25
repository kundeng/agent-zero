"""Tests for the srt sandbox backend's profile builder + session wiring.

Covers the three gaps that the user surfaced after the live demo:

1. **Whole-session wrap** — :class:`SandboxBackendSrt._SrtSession` builds
   its PTY command as ``srt --settings <profile> -- /bin/bash`` rather
   than wrapping each command. We can't actually run ``srt`` in CI
   (it's not in the test image and would need bubblewrap on Linux),
   so the test inspects the constructed command string instead.

2. **Per-project profile path** — :func:`_profile_path` returns
   ``<state_dir>/sandbox/<project>.json`` derived from the project
   folder's basename. Default for unbound.

3. **Network allowlist merge** — :func:`_build_profile` reads
   ``project.json``'s ``network.allow`` and merges with
   ``Settings.sandbox_network_default``. Both empty by default; union
   when both are present. Also asserts the deepcopy fix: building one
   profile must NOT mutate ``_DEFAULT_PROFILE``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hyperagent0.sandbox import srt as srt_backend


# ---------------------------------------------------------------------------
# Profile path
# ---------------------------------------------------------------------------


def test_profile_path_for_unbound_project(tmp_path, monkeypatch):
    """No project → ``<state_dir>/sandbox/default.json``."""

    monkeypatch.setenv("HYPERAGENT0_STATE_DIR", str(tmp_path))
    path = srt_backend._profile_path(None)
    assert path == tmp_path / "sandbox" / "default.json"


def test_profile_path_uses_project_basename(tmp_path, monkeypatch):
    """A project_dir → ``<state_dir>/sandbox/<basename>.json``.

    Per-project profile lets two projects with different network/FS
    policies coexist on the same host.
    """

    monkeypatch.setenv("HYPERAGENT0_STATE_DIR", str(tmp_path))
    pirate = "/foo/bar/usr/projects/pirate"
    eng = "/foo/bar/usr/projects/engineering"

    assert srt_backend._profile_path(pirate).name == "pirate.json"
    assert srt_backend._profile_path(eng).name == "engineering.json"


# ---------------------------------------------------------------------------
# Profile body — deepcopy + network merge
# ---------------------------------------------------------------------------


def test_build_profile_does_not_mutate_default():
    """Two builds in a row must leave ``_DEFAULT_PROFILE`` untouched.

    Earlier impl used a shallow copy and mutated the nested ``fs`` dict,
    so consecutive builds saw stale per-project paths from previous
    calls. Regression guard.
    """

    before = json.dumps(srt_backend._DEFAULT_PROFILE, sort_keys=True)
    srt_backend._build_profile("/tmp/proj-a")
    srt_backend._build_profile("/tmp/proj-b")
    after = json.dumps(srt_backend._DEFAULT_PROFILE, sort_keys=True)
    assert before == after, "_DEFAULT_PROFILE must remain pristine across builds"


def test_build_profile_unbound_keeps_defaults():
    profile = srt_backend._build_profile(None)
    assert profile["fs"]["read"]["deny"] == ["/etc/shadow"]
    assert profile["fs"]["read"]["allow"] == ["/"]
    assert profile["fs"]["write"]["allow"] == []
    assert profile["network"]["allow"] == []


def test_build_profile_bound_writes_only_under_project(tmp_path):
    project_dir = str(tmp_path / "pirate")
    Path(project_dir).mkdir()
    profile = srt_backend._build_profile(project_dir)
    assert profile["fs"]["write"]["allow"] == [project_dir]


def test_build_profile_merges_global_and_project_network_allow(tmp_path, monkeypatch):
    """Global ``sandbox_network_default`` ∪ project's ``network.allow``."""

    project_dir = tmp_path / "openai-research"
    (project_dir / ".a0proj").mkdir(parents=True)
    (project_dir / ".a0proj" / "project.json").write_text(
        json.dumps({"network": {"allow": ["api.openai.com", "openai.com"]}})
    )

    monkeypatch.setattr(
        srt_backend,
        "_global_network_default",
        lambda: ["api.anthropic.com", "github.com"],
    )

    profile = srt_backend._build_profile(str(project_dir))
    assert profile["network"]["allow"] == sorted(
        {"api.openai.com", "openai.com", "api.anthropic.com", "github.com"}
    )


def test_build_profile_tolerates_malformed_project_json(tmp_path, monkeypatch):
    """A broken project.json must not crash code-exec."""

    project_dir = tmp_path / "broken"
    (project_dir / ".a0proj").mkdir(parents=True)
    (project_dir / ".a0proj" / "project.json").write_text("not-json{{{")

    monkeypatch.setattr(srt_backend, "_global_network_default", lambda: [])
    profile = srt_backend._build_profile(str(project_dir))
    # Falls back to empty network policy — no exception.
    assert profile["network"]["allow"] == []


def test_global_network_default_returns_empty_when_settings_unavailable(monkeypatch):
    """``_global_network_default`` must swallow upstream settings errors.

    The sandbox should always come up — a broken settings layer
    shouldn't crash code-exec. The helper catches Exception and
    returns ``[]``. Simulate via a settings-module stub whose
    ``get_settings`` raises.
    """

    import types

    fake = types.ModuleType("python.helpers.settings")
    def _boom():  # noqa: D401
        raise RuntimeError("settings exploded")
    fake.get_settings = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "python.helpers.settings", fake)

    assert srt_backend._global_network_default() == []


# ---------------------------------------------------------------------------
# Disk write — _ensure_profile writes the composed JSON
# ---------------------------------------------------------------------------


def test_ensure_profile_writes_per_project_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERAGENT0_STATE_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "python.helpers.settings", MagicMock())

    project_dir = tmp_path / "pirate"
    project_dir.mkdir()
    path = srt_backend._ensure_profile(str(project_dir))

    assert path == tmp_path / "sandbox" / "pirate.json"
    on_disk = json.loads(path.read_text())
    assert on_disk["fs"]["write"]["allow"] == [str(project_dir)]


def test_ensure_profile_rewrites_on_subsequent_call(tmp_path, monkeypatch):
    """Settings/project.json changes between calls must propagate."""

    monkeypatch.setenv("HYPERAGENT0_STATE_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "python.helpers.settings", MagicMock())

    project_dir = tmp_path / "demo"
    (project_dir / ".a0proj").mkdir(parents=True)
    pj = project_dir / ".a0proj" / "project.json"
    pj.write_text(json.dumps({"network": {"allow": ["one.example"]}}))

    p1 = srt_backend._ensure_profile(str(project_dir))
    body1 = json.loads(p1.read_text())
    assert body1["network"]["allow"] == ["one.example"]

    # Operator edits project.json mid-session.
    pj.write_text(json.dumps({"network": {"allow": ["two.example"]}}))

    p2 = srt_backend._ensure_profile(str(project_dir))
    body2 = json.loads(p2.read_text())
    assert body2["network"]["allow"] == ["two.example"]


# ---------------------------------------------------------------------------
# Session wires srt around bash, not around each command
# ---------------------------------------------------------------------------


class _FakeTTYSessionModule:
    """Stand-in for ``python.helpers.tty_session`` that records the cmd
    passed to ``TTYSession`` and captures ``sendline`` calls."""

    def __init__(self):
        self.opened_with: dict = {}
        self.sent: list[str] = []

        outer = self

        class _FakeTTYSession:
            def __init__(self, cmd, *, cwd=None, **kwargs):
                outer.opened_with["cmd"] = cmd
                outer.opened_with["cwd"] = cwd

            async def start(self):
                return None

            async def read_full_until_idle(self, **kwargs):
                return ""

            async def sendline(self, line: str):
                outer.sent.append(line)

            async def close(self):
                return None

        self.TTYSession = _FakeTTYSession


def _inject_fake_tty_session(monkeypatch) -> _FakeTTYSessionModule:
    """Pre-populate ``srt_backend.tty_session`` so the lazy loader is a no-op
    and tests don't pull in the real ``python.helpers.tty_session`` (which
    crashes under pytest's stdin stub)."""

    fake = _FakeTTYSessionModule()
    monkeypatch.setattr(srt_backend, "tty_session", fake)
    return fake


def test_srt_session_connect_invokes_srt_wrapping_bash(tmp_path, monkeypatch):
    """``_SrtSession.connect`` must build a TTYSession whose command is
    ``srt --settings <profile> -- /bin/bash`` — wrapping the shell, NOT
    individual commands."""

    fake = _inject_fake_tty_session(monkeypatch)

    profile_path = tmp_path / "pirate.json"
    profile_path.write_text("{}")

    session = srt_backend._SrtSession(cwd="/tmp/work", profile_path=profile_path)
    asyncio.run(session.connect())

    cmd = fake.opened_with["cmd"]
    assert cmd.startswith("srt --settings ")
    assert "-- /bin/bash" in cmd
    assert str(profile_path) in cmd
    assert fake.opened_with["cwd"] == "/tmp/work"


def test_srt_session_does_not_per_command_wrap(tmp_path, monkeypatch):
    """``_SrtSession.send_command`` must NOT prepend ``srt`` per command —
    that was the previous (broken) behavior that ate ``cd`` and ``export``.
    Verifies the raw command goes through to the underlying TTYSession."""

    fake = _inject_fake_tty_session(monkeypatch)

    profile_path = tmp_path / "default.json"
    profile_path.write_text("{}")

    session = srt_backend._SrtSession(cwd=None, profile_path=profile_path)
    asyncio.run(session.connect())
    asyncio.run(session.send_command("cd /tmp"))
    asyncio.run(session.send_command("export FOO=bar"))
    asyncio.run(session.send_command("ls *.py"))

    # Each command went through verbatim. No ``srt --settings ... --`` prefix.
    assert fake.sent == ["cd /tmp", "export FOO=bar", "ls *.py"]
    assert all(not s.startswith("srt --settings") for s in fake.sent)


# ---------------------------------------------------------------------------
# get_backend plumbs project_dir
# ---------------------------------------------------------------------------


def test_get_backend_passes_project_dir():
    """``get_backend(mode, project_dir=...)`` must propagate the kwarg
    so the backend's ``_ensure_profile`` lands at the per-project file."""

    from hyperagent0.sandbox import get_backend

    backend = get_backend("none", project_dir="/path/to/proj")
    assert backend.project_dir == "/path/to/proj"
