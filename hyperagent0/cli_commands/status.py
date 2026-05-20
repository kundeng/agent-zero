"""``haz status`` — report whether the daemon is running.

Cold-start sensitive (spec 03 D5). This command MUST NOT import
LiteLLM, channel SDKs, Flask, or anything else heavy. It reads the PID
file, signals the process with ``kill -0`` to confirm liveness, and
optionally probes a Unix socket at ``~/.hyperagent0/daemon.sock`` for
live counts. When the socket is not present (daemon down or socket
not yet implemented) the command falls back to PID-only output.
"""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any

import click

from .. import daemon as _daemon


def _proc_start_time(pid: int) -> float | None:
    """Best-effort process start time (epoch seconds) via ``/proc``.

    Linux-only; we silently fall back to ``None`` on other platforms.
    """

    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("latin-1")
    except OSError:
        return None
    # /proc/[pid]/stat field 22 is starttime in clock ticks since boot,
    # but the comm field can contain spaces in parens. Split from the
    # right of ')' to avoid that landmine.
    try:
        rparen = data.rindex(")")
    except ValueError:
        return None
    fields = data[rparen + 2 :].split()
    # field 22 in the man page; with comm/state stripped that's index 19.
    if len(fields) < 20:
        return None
    try:
        starttime_ticks = int(fields[19])
    except ValueError:
        return None
    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        clk_tck = 100
    try:
        with open("/proc/uptime", "rb") as f:
            uptime_secs = float(f.read().split()[0])
    except OSError:
        return None
    boot_time = time.time() - uptime_secs
    return boot_time + (starttime_ticks / clk_tck)


def _format_uptime(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _query_socket(timeout: float = 0.2) -> dict[str, Any] | None:
    """Ask the daemon for live state over the Unix socket.

    The daemon doesn't yet serve this socket (P2/P3 work); this is the
    forward-compatible probe so ``haz status`` can light up extra
    fields once the daemon side lands.
    """

    path = _daemon.sock_file()
    if not path.exists():
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(path))
        s.sendall(b'{"cmd":"status"}\n')
        chunks: list[bytes] = []
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        s.close()
    except OSError:
        return None
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _collect_status() -> dict[str, Any]:
    running = _daemon.is_running()
    pid = _daemon.get_pid() if running else None
    info: dict[str, Any] = {
        "running": running,
        "pid": pid,
        "state_dir": str(_daemon.state_dir()),
        "pid_file": str(_daemon.pid_file()),
        "lock_file": str(_daemon.lock_file()),
    }
    if running and pid is not None:
        start = _proc_start_time(pid)
        if start is not None:
            info["start_time_epoch"] = start
            info["uptime_seconds"] = max(0.0, time.time() - start)
        live = _query_socket()
        if live:
            info["live"] = live
    return info


@click.command("status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of human-friendly text.",
)
def command(as_json: bool) -> None:
    """Show daemon status (running/stopped, PID, uptime)."""

    info = _collect_status()

    if as_json:
        click.echo(json.dumps(info, indent=2, sort_keys=True))
        return

    if info["running"]:
        pid = info["pid"]
        uptime = info.get("uptime_seconds")
        uptime_str = f", uptime {_format_uptime(uptime)}" if uptime is not None else ""
        click.echo(f"hyperagent0: running (PID {pid}{uptime_str})")
        live = info.get("live") or {}
        if live:
            host = live.get("host")
            port = live.get("port")
            ctxs = live.get("active_contexts")
            if host and port:
                click.echo(f"  bind: http://{host}:{port}")
            if ctxs is not None:
                click.echo(f"  active contexts: {ctxs}")
    else:
        click.echo("hyperagent0: not running.")
    click.echo(f"  state dir: {info['state_dir']}")
