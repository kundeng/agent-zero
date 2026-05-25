"""``haz install-service`` / ``haz uninstall-service`` (spec 03 task 2.4).

Generates and activates a per-user OS service that runs ``haz start
--systemd`` on boot:

* **Linux** → systemd user unit at
  ``~/.config/systemd/user/hyperagent0.service`` (matches spec 03 D3).
* **macOS** → launchd LaunchAgent at
  ``~/Library/LaunchAgents/com.hyperagent0.daemon.plist``.

Both flavors are per-user, so root is never required. Both target the
same ``--systemd`` invocation (foreground, no double-fork, no PID file
write — the supervisor owns process lifecycle).

Self-contained: imports stdlib only at module load so ``haz --help``
remains under the cold-start budget (spec 03 D5). Heavy work
(``shutil.which``, file writes, ``subprocess.run``) happens inside the
command body.
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from pathlib import Path

import click


_LINUX_UNIT_NAME = "hyperagent0.service"
_LINUX_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

_MACOS_LABEL = "com.hyperagent0.daemon"
_MACOS_PLIST_DIR = Path.home() / "Library" / "LaunchAgents"


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def _detect_platform() -> str:
    """Return ``"linux"``, ``"macos"``, or raise UsageError."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise click.UsageError(
        f"haz install-service is not supported on platform {sys.platform!r}. "
        "Use `haz start -d` to daemonize manually."
    )


def _resolve_haz_binary() -> Path:
    """Find the absolute path to the ``haz`` binary.

    Service units / plists must use an absolute path because they run
    without a user shell on boot. ``shutil.which`` consults PATH; we
    fall back to ``sys.argv[0]`` if PATH is empty (happens in some CI).
    """

    found = shutil.which("haz") or shutil.which("hyperagent0")
    if found:
        return Path(found).resolve()
    argv0 = Path(sys.argv[0]).resolve()
    if argv0.exists():
        return argv0
    raise click.ClickException(
        "Could not locate the haz binary. Run `pip install hyperagent0` or "
        "re-run install.sh first."
    )


# ---------------------------------------------------------------------------
# Unit-file rendering
# ---------------------------------------------------------------------------


def _render_systemd_unit(haz_path: Path) -> str:
    return textwrap.dedent(
        f"""\
        [Unit]
        Description=HyperAgent Zero daemon
        After=network.target

        [Service]
        Type=exec
        ExecStart={haz_path} start --systemd
        Restart=on-failure
        RestartSec=5
        # Journald is the default. Override via environment if you need
        # file logs (e.g. StandardOutput=append:/var/log/hyperagent0.log).
        Environment=PYTHONUNBUFFERED=1

        [Install]
        WantedBy=default.target
        """
    )


def _render_launchd_plist(haz_path: Path) -> str:
    # KeepAlive.SuccessfulExit=false → restart on crash, not on clean
    # exit. RunAtLoad=true → start at user login (the LaunchAgent
    # equivalent of `systemctl --user enable`).
    log_path = Path.home() / ".hyperagent0" / "logs" / "daemon.log"
    return textwrap.dedent(
        f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{_MACOS_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{haz_path}</string>
                <string>start</string>
                <string>--systemd</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <dict>
                <key>SuccessfulExit</key>
                <false/>
            </dict>
            <key>StandardOutPath</key>
            <string>{log_path}</string>
            <key>StandardErrorPath</key>
            <string>{log_path}</string>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PYTHONUNBUFFERED</key>
                <string>1</string>
            </dict>
        </dict>
        </plist>
        """
    )


# ---------------------------------------------------------------------------
# Install / uninstall actions
# ---------------------------------------------------------------------------


def _systemd_user_command(args: list[str]) -> int:
    """Run ``systemctl --user ...`` and return its exit code.

    Imported lazily so ``haz install-service`` on macOS doesn't pull
    subprocess at module load (subprocess is small but the principle
    matters for the cold-start budget).
    """
    import subprocess

    proc = subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        click.echo(proc.stdout.rstrip())
    if proc.stderr:
        click.echo(proc.stderr.rstrip(), err=True)
    return proc.returncode


def _install_linux() -> None:
    haz_path = _resolve_haz_binary()
    _LINUX_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    unit_path = _LINUX_UNIT_DIR / _LINUX_UNIT_NAME
    unit_path.write_text(_render_systemd_unit(haz_path), encoding="utf-8")
    click.echo(f"wrote {unit_path}")

    if shutil.which("systemctl") is None:
        click.echo(
            "systemctl not found — unit written but not activated. "
            "Install systemd to enable it, or run the daemon manually with "
            "`haz start`.",
            err=True,
        )
        return

    _systemd_user_command(["daemon-reload"])
    rc = _systemd_user_command(["enable", "--now", _LINUX_UNIT_NAME])
    if rc != 0:
        raise click.ClickException(
            f"`systemctl --user enable --now {_LINUX_UNIT_NAME}` failed. "
            "Check `systemctl --user status hyperagent0` for the cause."
        )
    click.echo(f"hyperagent0 service is now active (systemd user unit).")


def _uninstall_linux() -> None:
    unit_path = _LINUX_UNIT_DIR / _LINUX_UNIT_NAME
    if shutil.which("systemctl") is not None and unit_path.exists():
        _systemd_user_command(["disable", "--now", _LINUX_UNIT_NAME])
    if unit_path.exists():
        unit_path.unlink()
        click.echo(f"removed {unit_path}")
    else:
        click.echo(f"no unit file at {unit_path}; nothing to remove")
    if shutil.which("systemctl") is not None:
        _systemd_user_command(["daemon-reload"])


def _launchctl_command(args: list[str], *, check_rc: bool = False) -> int:
    import subprocess

    proc = subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        click.echo(proc.stdout.rstrip())
    if proc.stderr and proc.returncode != 0:
        # launchctl is chatty on stderr even on success; only surface
        # error output when the command actually failed.
        click.echo(proc.stderr.rstrip(), err=True)
    if check_rc and proc.returncode != 0:
        raise click.ClickException(
            f"launchctl {' '.join(args)} exited with code {proc.returncode}"
        )
    return proc.returncode


def _install_macos() -> None:
    haz_path = _resolve_haz_binary()
    _MACOS_PLIST_DIR.mkdir(parents=True, exist_ok=True)
    plist_path = _MACOS_PLIST_DIR / f"{_MACOS_LABEL}.plist"
    plist_path.write_text(_render_launchd_plist(haz_path), encoding="utf-8")
    click.echo(f"wrote {plist_path}")

    # Make sure the daemon log directory exists before launchd opens it.
    (Path.home() / ".hyperagent0" / "logs").mkdir(parents=True, exist_ok=True)

    if shutil.which("launchctl") is None:
        click.echo(
            "launchctl not found — plist written but not activated. "
            "Run `launchctl bootstrap gui/$(id -u) <plist>` manually.",
            err=True,
        )
        return

    uid = os.getuid()
    target = f"gui/{uid}"
    # ``bootstrap`` registers the service with the user's domain so it
    # auto-starts at next login. ``kickstart`` starts it right now.
    # Bootout first (idempotent — silent error if not loaded).
    _launchctl_command(["bootout", f"{target}/{_MACOS_LABEL}"])
    _launchctl_command(["bootstrap", target, str(plist_path)], check_rc=True)
    _launchctl_command(["kickstart", "-k", f"{target}/{_MACOS_LABEL}"])
    click.echo("hyperagent0 service is now active (launchd LaunchAgent).")


def _uninstall_macos() -> None:
    plist_path = _MACOS_PLIST_DIR / f"{_MACOS_LABEL}.plist"
    if shutil.which("launchctl") is not None and plist_path.exists():
        uid = os.getuid()
        _launchctl_command(["bootout", f"gui/{uid}/{_MACOS_LABEL}"])
    if plist_path.exists():
        plist_path.unlink()
        click.echo(f"removed {plist_path}")
    else:
        click.echo(f"no plist at {plist_path}; nothing to remove")


# ---------------------------------------------------------------------------
# Click commands
# ---------------------------------------------------------------------------


@click.command("install-service")
def command() -> None:
    """Install a per-user OS service that runs `haz start --systemd` at boot.

    Linux: systemd user unit at ``~/.config/systemd/user/hyperagent0.service``.
    macOS: launchd LaunchAgent at
    ``~/Library/LaunchAgents/com.hyperagent0.daemon.plist``.

    No root required. Run ``haz uninstall-service`` to remove.
    """

    platform = _detect_platform()
    if platform == "linux":
        _install_linux()
    else:
        _install_macos()


@click.command("uninstall-service")
def uninstall_command() -> None:
    """Remove the per-user OS service installed by ``haz install-service``."""

    platform = _detect_platform()
    if platform == "linux":
        _uninstall_linux()
    else:
        _uninstall_macos()
