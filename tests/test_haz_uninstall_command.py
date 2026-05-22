"""Unit tests for ``haz uninstall``.

The command rmtrees real directories, so every test scopes the
``--prefix`` flag to a tmpdir. We never let the test touch the real
``~/.hyperagent0``. Daemon-state probes are stubbed so we don't depend
on a live daemon.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from hyperagent0.cli_commands import uninstall as uninstall_cmd


def _fake_install(prefix: Path) -> None:
    """Build a directory layout that looks like a real install."""

    (prefix / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    (prefix / "venv" / "bin" / "haz").write_text("#!/usr/bin/env python3\n")
    (prefix / "venv" / "bin" / "haz").chmod(0o755)
    (prefix / "venv" / "lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    (prefix / "venv" / "lib" / "site-packages" / "hyperagent0_repo.pth").write_text("")
    (prefix / "repo").mkdir(parents=True, exist_ok=True)
    (prefix / "repo" / "agent.py").write_text("# stub\n")
    (prefix / "repo" / "pyproject.toml").write_text("[project]\nname='hyperagent0'\n")
    (prefix / "repo" / "usr").mkdir(parents=True, exist_ok=True)
    (prefix / "repo" / "usr" / "settings.json").write_text(json.dumps({"sandbox_mode": "none"}))
    (prefix / "logs").mkdir(parents=True, exist_ok=True)
    (prefix / "logs" / "daemon.log").write_text("INFO: stub log\n")


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    """A throwaway install layout + daemon stubs."""

    prefix = tmp_path / "haz-install"
    _fake_install(prefix)
    # Pretend the daemon is never running so uninstall doesn't try to
    # signal a PID it doesn't own.
    monkeypatch.setattr(uninstall_cmd._daemon, "is_running", lambda: False)
    return prefix


def _exists(*paths: Path) -> list[bool]:
    return [p.exists() for p in paths]


def test_default_uninstall_removes_everything(fake_install):
    runner = CliRunner()
    result = runner.invoke(
        uninstall_cmd.command,
        ["--prefix", str(fake_install), "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert not (fake_install / "venv").exists()
    assert not (fake_install / "repo").exists()
    assert not (fake_install / "logs").exists()


def test_keep_state_preserves_usr_and_logs(fake_install):
    runner = CliRunner()
    result = runner.invoke(
        uninstall_cmd.command,
        ["--prefix", str(fake_install), "--yes", "--keep-state"],
    )
    assert result.exit_code == 0, result.output
    # Venv is gone, but state survives.
    assert not (fake_install / "venv").exists()
    assert (fake_install / "logs").exists()
    assert (fake_install / "repo" / "usr" / "settings.json").exists()
    # The non-state parts of the repo (agent.py, pyproject) are gone so
    # a fresh re-install gets a clean checkout.
    assert not (fake_install / "repo" / "agent.py").exists()
    assert not (fake_install / "repo" / "pyproject.toml").exists()


def test_refuses_unknown_directory(tmp_path):
    """Sanity: refuse to nuke a directory that isn't ours."""

    other = tmp_path / "definitely-not-haz"
    other.mkdir()
    (other / "important-file.txt").write_text("don't delete me")

    runner = CliRunner()
    result = runner.invoke(
        uninstall_cmd.command,
        ["--prefix", str(other), "--yes"],
    )
    assert result.exit_code == 2
    assert (other / "important-file.txt").exists()


def test_removes_only_symlinks_pointing_into_prefix(tmp_path, monkeypatch):
    """The symlink scrub must NOT remove a ``haz`` link that points
    somewhere else (e.g., a separate --dev install on the same box)."""

    prefix = tmp_path / "install-a"
    _fake_install(prefix)

    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir()
    own_link = bin_dir / "haz"
    own_link.symlink_to(prefix / "venv" / "bin" / "haz")
    other_link = bin_dir / "hyperagent0"
    # Points at a *different* install — must be left alone.
    other_install = tmp_path / "install-b"
    _fake_install(other_install)
    other_link.symlink_to(other_install / "venv" / "bin" / "haz")

    monkeypatch.setattr(uninstall_cmd, "_bin_links", lambda: [own_link, other_link])
    monkeypatch.setattr(uninstall_cmd._daemon, "is_running", lambda: False)

    runner = CliRunner()
    result = runner.invoke(
        uninstall_cmd.command,
        ["--prefix", str(prefix), "--yes"],
    )
    assert result.exit_code == 0, result.output
    # Own link gone, the other one survives.
    assert not own_link.exists() and not own_link.is_symlink()
    assert other_link.is_symlink()
    # The other install is untouched.
    assert (other_install / "venv" / "bin" / "haz").is_file()


def test_confirmation_aborts_without_yes(fake_install):
    runner = CliRunner()
    # Empty stdin -> Click confirms gets EOF -> abort.
    result = runner.invoke(
        uninstall_cmd.command,
        ["--prefix", str(fake_install)],
        input="n\n",
    )
    assert result.exit_code != 0
    # Nothing was removed.
    assert (fake_install / "venv").exists()
    assert (fake_install / "repo").exists()


def test_dry_summary_lists_target_dirs(fake_install):
    runner = CliRunner()
    result = runner.invoke(
        uninstall_cmd.command,
        ["--prefix", str(fake_install), "--yes"],
    )
    # The output should name what's about to be removed so the user
    # knows what they're agreeing to.
    assert "venv" in result.output
    assert "repo" in result.output
