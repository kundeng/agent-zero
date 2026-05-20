"""``haz restart`` — convenience wrapper around stop + start.

The two phases are independent commands so they can be invoked
directly; ``restart`` exists to give users the obvious one-liner.
"""

from __future__ import annotations

import time

import click

from .. import daemon as _daemon
from . import start as _start
from . import stop as _stop


@click.command("restart")
@click.option(
    "--timeout",
    type=float,
    default=30.0,
    show_default=True,
    help="Graceful shutdown timeout for the stop phase.",
)
@click.option(
    "-d",
    "--daemon",
    "daemonize",
    is_flag=True,
    default=False,
    help="After stopping, re-launch in background daemon mode (-d on start).",
)
@click.option("--host", default=None, help="Bind host to pass to 'start'.")
@click.option("--port", type=int, default=None, help="Bind port to pass to 'start'.")
@click.pass_context
def command(
    ctx: click.Context,
    timeout: float,
    daemonize: bool,
    host: str | None,
    port: int | None,
) -> None:
    """Stop the daemon (if running) and start it again."""

    if _daemon.is_running():
        ctx.invoke(_stop.command, timeout=timeout, force=False)
        # Give the OS a moment to release the lock fd before the new
        # process tries to grab it.
        for _ in range(20):
            if not _daemon.is_running():
                break
            time.sleep(0.1)
    else:
        click.echo("hyperagent0: not running; starting fresh.")

    ctx.invoke(
        _start.command,
        daemonize=daemonize,
        systemd=False,
        host=host,
        port=port,
    )
