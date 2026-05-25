"""Tests for ``haz install-service`` / ``haz uninstall-service`` (spec 03 task 2.4).

The unit / plist rendering is pure-string and tested directly. The
install actions are integration-style: we monkeypatch ``shutil.which``
and ``subprocess.run`` so they never touch the real systemd or
launchd.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import click
import pytest


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ``Path.home()`` so unit/plist writes land in tmp."""

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # Re-import to recompute module-level paths derived from Path.home().
    import importlib

    import hyperagent0.cli_commands.install_service as svc

    importlib.reload(svc)
    return tmp_path, svc


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def test_detect_platform_linux(monkeypatch, fake_home):
    _, svc = fake_home
    monkeypatch.setattr(sys, "platform", "linux")
    assert svc._detect_platform() == "linux"


def test_detect_platform_macos(monkeypatch, fake_home):
    _, svc = fake_home
    monkeypatch.setattr(sys, "platform", "darwin")
    assert svc._detect_platform() == "macos"


def test_detect_platform_unsupported_raises(monkeypatch, fake_home):
    _, svc = fake_home
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(click.UsageError):
        svc._detect_platform()


# ---------------------------------------------------------------------------
# Unit / plist rendering — pure-string, no I/O
# ---------------------------------------------------------------------------


def test_render_systemd_unit_uses_absolute_haz_path(fake_home):
    _, svc = fake_home
    unit = svc._render_systemd_unit(Path("/usr/local/bin/haz"))

    # Required systemd directives present:
    assert "[Unit]" in unit
    assert "[Service]" in unit
    assert "[Install]" in unit
    assert "ExecStart=/usr/local/bin/haz start --systemd" in unit
    assert "Type=exec" in unit
    assert "WantedBy=default.target" in unit
    # The --systemd flag tells haz to skip daemonization (systemd owns
    # the PID per spec 03 D6) — make sure we don't accidentally pass -d.
    assert " -d " not in unit and "--daemon" not in unit


def test_render_launchd_plist_includes_label_and_args(fake_home):
    home, svc = fake_home
    plist = svc._render_launchd_plist(Path("/opt/homebrew/bin/haz"))

    assert "com.hyperagent0.daemon" in plist
    assert "/opt/homebrew/bin/haz" in plist
    assert "<string>start</string>" in plist
    assert "<string>--systemd</string>" in plist
    # Restart-on-crash semantics (KeepAlive.SuccessfulExit=false).
    assert "<key>KeepAlive</key>" in plist
    assert "<key>SuccessfulExit</key>" in plist
    # StandardOutPath under the user's home (~/.hyperagent0/logs/...).
    assert str(home / ".hyperagent0" / "logs" / "daemon.log") in plist


# ---------------------------------------------------------------------------
# Install actions — monkeypatch shutil + subprocess
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_haz_binary(monkeypatch, fake_home):
    home, svc = fake_home
    fake = home / "bin" / "haz"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    import shutil

    monkeypatch.setattr(svc, "shutil", shutil)
    monkeypatch.setattr(shutil, "which", lambda name: str(fake) if name in ("haz", "hyperagent0", "systemctl", "launchctl") else None)
    return home, svc, fake


def test_install_linux_writes_unit_and_invokes_systemctl(monkeypatch, fake_haz_binary):
    home, svc, fake_haz = fake_haz_binary
    monkeypatch.setattr(sys, "platform", "linux")

    calls: list[list[str]] = []

    class _StubResult:
        stdout = ""
        stderr = ""
        returncode = 0

    def _stub_run(cmd, capture_output, text):
        calls.append(list(cmd))
        return _StubResult()

    import subprocess

    monkeypatch.setattr(subprocess, "run", _stub_run)

    svc._install_linux()

    unit_path = home / ".config" / "systemd" / "user" / "hyperagent0.service"
    assert unit_path.is_file()
    unit = unit_path.read_text()
    assert f"ExecStart={fake_haz} start --systemd" in unit
    # daemon-reload + enable --now
    assert calls[0][:3] == ["systemctl", "--user", "daemon-reload"]
    assert calls[1][:4] == ["systemctl", "--user", "enable", "--now"]


def test_install_macos_writes_plist_and_bootstraps_launchctl(monkeypatch, fake_haz_binary):
    home, svc, fake_haz = fake_haz_binary
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)

    calls: list[list[str]] = []

    class _StubResult:
        stdout = ""
        stderr = ""
        returncode = 0

    def _stub_run(cmd, capture_output, text):
        calls.append(list(cmd))
        return _StubResult()

    import subprocess

    monkeypatch.setattr(subprocess, "run", _stub_run)

    svc._install_macos()

    plist_path = home / "Library" / "LaunchAgents" / "com.hyperagent0.daemon.plist"
    assert plist_path.is_file()
    body = plist_path.read_text()
    assert str(fake_haz) in body

    # bootout (idempotent), bootstrap, kickstart — in that order.
    sequence = [c[1] for c in calls if c[0] == "launchctl"]
    assert sequence == ["bootout", "bootstrap", "kickstart"]
    # bootstrap target is gui/<uid>
    bootstrap = next(c for c in calls if c[0] == "launchctl" and c[1] == "bootstrap")
    assert bootstrap[2] == "gui/501"


def test_uninstall_linux_removes_unit_and_disables(monkeypatch, fake_haz_binary):
    home, svc, _ = fake_haz_binary
    monkeypatch.setattr(sys, "platform", "linux")

    unit_path = home / ".config" / "systemd" / "user" / "hyperagent0.service"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text("# placeholder")

    calls: list[list[str]] = []

    class _StubResult:
        stdout = ""
        stderr = ""
        returncode = 0

    import subprocess

    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: (calls.append(list(cmd)), _StubResult())[1]
    )

    svc._uninstall_linux()
    assert not unit_path.exists()
    # disable --now + daemon-reload at minimum.
    flat = [item for c in calls for item in c]
    assert "disable" in flat
    assert "daemon-reload" in flat


def test_uninstall_macos_removes_plist(monkeypatch, fake_haz_binary):
    home, svc, _ = fake_haz_binary
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)

    plist_path = home / "Library" / "LaunchAgents" / "com.hyperagent0.daemon.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text("<placeholder/>")

    import subprocess

    calls: list[list[str]] = []

    class _StubResult:
        stdout = ""
        stderr = ""
        returncode = 0

    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: (calls.append(list(cmd)), _StubResult())[1]
    )

    svc._uninstall_macos()
    assert not plist_path.exists()
    # bootout was called.
    assert any(c[:2] == ["launchctl", "bootout"] for c in calls)
