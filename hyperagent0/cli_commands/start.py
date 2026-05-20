"""``haz start`` — launch the hyperagent0 daemon (foreground by default).

Behavior summary (spec 03 D6, task 1.4):

* Default: foreground. Logs stream to stdout, ``Ctrl-C`` triggers a
  graceful shutdown via the signal handler installed below.
* ``-d`` / ``--daemon``: classic Unix double-fork, redirect stdout and
  stderr into ``~/.hyperagent0/logs/daemon.log``, return to the shell
  once the child has acquired the singleton lock.
* ``--systemd``: foreground (systemd manages the lifecycle), no
  daemonization, no PID file write — systemd owns the PID.

All heavy imports (``run_ui.start_server``, Flask, LiteLLM) happen
inside :func:`_run_server`. Keeping them out of the module body
preserves the cold-start budget for everyone who imports this file
just to register the Click command via :mod:`hyperagent0.cli`.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import click

from .. import daemon as _daemon


def _install_signal_handlers(lock_handle) -> None:
    """Install SIGTERM/SIGINT handlers that drive a graceful shutdown.

    Spec 03 task 1.6: pause all AgentContexts, wait for in-flight tools,
    persist state, close connections. We delegate to
    :func:`hyperagent0.shutdown.graceful_shutdown` so the hook stays out
    of the agent core (no agent.py patch needed).
    """

    _shutting_down = {"flag": False}

    def _handler(signum, _frame):  # noqa: ANN001 - signal handler signature
        if _shutting_down["flag"]:
            # Second signal — escalate.
            click.echo("[hyperagent0] forced exit", err=True)
            os._exit(1)
        _shutting_down["flag"] = True
        signame = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        click.echo(f"[hyperagent0] received {signame}, shutting down...", err=True)
        # Import here so even SIGTERM handling stays out of the cold
        # path until the daemon is actually live.
        try:
            from ..shutdown import graceful_shutdown

            graceful_shutdown(timeout=25.0)
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            click.echo(f"[hyperagent0] shutdown hook error: {exc}", err=True)
        finally:
            _daemon.release_lock(lock_handle)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _run_server(host: str | None, port: int | None) -> None:
    """Call into ``run_ui.start_server`` with the given overrides.

    Imports inside the function body — see module docstring.
    """

    # The CLI lives inside the hyperagent0 wrapper package; ``run_ui`` is
    # the upstream Agent Zero entry point at the repo root. Make sure
    # the repo root is importable when the CLI is invoked from an
    # arbitrary cwd via a console_script.
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import run_ui  # type: ignore[import-not-found]

    run_ui.start_server(host=host, port=port)


def _daemonize(log_path: Path) -> None:
    """Classic Unix double-fork. The grandchild returns; everything
    else exits. Stdout/stderr are redirected to ``log_path``."""

    # First fork.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    # Second fork — orphan from the controlling terminal.
    if os.fork() > 0:
        os._exit(0)

    os.chdir("/")
    os.umask(0o022)

    # Reopen stdio. /dev/null for stdin, log file for stdout/stderr.
    sys.stdout.flush()
    sys.stderr.flush()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(devnull_fd, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(devnull_fd)
    os.close(log_fd)


@click.command("start")
@click.option(
    "-d",
    "--daemon",
    "daemonize",
    is_flag=True,
    default=False,
    help="Double-fork and run as a background daemon (logs redirected).",
)
@click.option(
    "--systemd",
    is_flag=True,
    default=False,
    help="Foreground systemd-friendly mode: no daemonization, no PID file write.",
)
@click.option(
    "--host",
    default=None,
    metavar="HOST",
    help="Bind host (default: localhost or WEB_UI_HOST env).",
)
@click.option(
    "--port",
    type=int,
    default=None,
    metavar="PORT",
    help="Bind port (default: settings/env WEB_UI_PORT).",
)
def command(daemonize: bool, systemd: bool, host: str | None, port: int | None) -> None:
    """Launch the hyperagent0 daemon (web UI + agent runtime).

    Default is foreground — ``Ctrl-C`` stops cleanly. Pass ``-d`` to
    detach into a background daemon, or ``--systemd`` when running
    under a systemd unit.
    """

    if daemonize and systemd:
        raise click.UsageError("--daemon and --systemd are mutually exclusive.")

    if systemd:
        # Systemd owns the PID and lifecycle; no lock/pidfile here.
        _install_signal_handlers(None)
        _run_server(host, port)
        return

    # Idempotency: if a daemon is already running, surface its status
    # and exit 0 rather than racing on the lock.
    if _daemon.is_running():
        pid = _daemon.get_pid()
        click.echo(
            f"hyperagent0 is already running (PID {pid}). Run 'haz status' for details."
        )
        return

    if daemonize:
        log_path = _daemon.logs_dir() / "daemon.log"
        click.echo(f"hyperagent0: detaching; logs -> {log_path}")
        _daemonize(log_path)
        # In the grandchild now. Acquire the lock and run the server.
        handle = _daemon.acquire_lock()
        if handle is None:
            # Lost the race with a parallel start.
            sys.exit(0)
        _install_signal_handlers(handle)
        try:
            _run_server(host, port)
        finally:
            _daemon.release_lock(handle)
        return

    # Foreground.
    handle = _daemon.acquire_lock()
    if handle is None:
        pid = _daemon.get_pid()
        click.echo(
            f"hyperagent0 is already running (PID {pid}). Run 'haz status' for details."
        )
        return
    _install_signal_handlers(handle)
    click.echo(f"hyperagent0: starting (PID {os.getpid()})")
    try:
        _run_server(host, port)
    finally:
        _daemon.release_lock(handle)
