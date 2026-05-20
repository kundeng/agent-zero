"""``haz stop`` — terminate a running hyperagent0 daemon.

Sends ``SIGTERM`` to the daemon recorded in the PID file and waits up
to ``--timeout`` seconds for graceful shutdown. Idempotent: if no
daemon is running, prints a short message and exits with status 0
(spec 03 task 1.5).
"""

from __future__ import annotations

import signal

import click

from .. import daemon as _daemon


@click.command("stop")
@click.option(
    "--timeout",
    type=float,
    default=30.0,
    show_default=True,
    metavar="SECONDS",
    help="How long to wait for graceful shutdown before reporting.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="If the daemon does not exit within --timeout, send SIGKILL.",
)
def command(timeout: float, force: bool) -> None:
    """Stop the running hyperagent0 daemon."""

    if not _daemon.is_running():
        click.echo("hyperagent0: not running.")
        # Best-effort cleanup of an orphan PID file.
        try:
            _daemon.pid_file().unlink(missing_ok=True)
        except OSError:
            pass
        return

    pid = _daemon.get_pid()
    if pid is None:
        click.echo("hyperagent0: lock held but PID file unreadable; nothing to do.")
        return

    click.echo(f"hyperagent0: sending SIGTERM to PID {pid} (timeout {timeout:.0f}s)...")
    if not _daemon.send_signal(signal.SIGTERM):
        click.echo("hyperagent0: failed to deliver SIGTERM (already exited?).")
        return

    if _daemon.wait_for_exit(timeout=timeout):
        click.echo("hyperagent0: stopped.")
        return

    if force:
        click.echo("hyperagent0: graceful shutdown timed out; sending SIGKILL...")
        _daemon.send_signal(signal.SIGKILL)
        if _daemon.wait_for_exit(timeout=5.0):
            click.echo("hyperagent0: killed.")
            return
        click.echo("hyperagent0: process did not exit even after SIGKILL.", err=True)
        raise click.exceptions.Exit(1)

    click.echo(
        "hyperagent0: graceful shutdown timed out. Re-run with --force to SIGKILL.",
        err=True,
    )
    raise click.exceptions.Exit(1)
