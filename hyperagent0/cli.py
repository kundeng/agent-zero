"""Top-level Click group for the ``hyperagent0`` / ``haz`` CLI.

This module is the single entry point for both binaries registered in
``pyproject.toml``. To honor the cold-start budget defined in spec 03
(D5: ``haz --help`` and ``haz status`` must return in <200ms), this
module imports only ``click`` and standard library modules. Subcommand
bodies live under :mod:`hyperagent0.cli_commands` and are loaded on
demand via :class:`LazyGroup`.

D4: a bare ``haz`` invocation does NOT silently start the daemon. It
prints status output plus a one-line hint pointing users at
``haz start``.
"""

from __future__ import annotations

import importlib
from typing import Any

import click

from . import __version__
from . import paths as _paths

# Make ``agent.py`` and friends importable for subcommands that need them
# (``haz start`` chiefly). Stdlib-only, cached, and a no-op if the repo
# can't be located — so we don't break ``haz --help`` for users who
# haven't run install.sh yet.
_paths.ensure_on_syspath()

# Map of subcommand name -> module under hyperagent0.cli_commands.
# Each module must expose a top-level Click ``Command`` named ``command``.
# Keep this table small and ordered: it directly controls the help output
# and is the only place that lists every shipped subcommand.
_SUBCOMMANDS: dict[str, str] = {
    "start": "hyperagent0.cli_commands.start",
    "stop": "hyperagent0.cli_commands.stop",
    "restart": "hyperagent0.cli_commands.restart",
    "status": "hyperagent0.cli_commands.status",
    "logs": "hyperagent0.cli_commands.logs",
    "setup": "hyperagent0.cli_commands.setup",
    "config": "hyperagent0.cli_commands.config",
}


class LazyGroup(click.Group):
    """A :class:`click.Group` that imports subcommand modules lazily.

    The standard :class:`click.Group` requires subcommands to be added at
    group construction time, which would force eager imports of every
    subcommand's dependencies (Flask, channel SDKs, LiteLLM, ...) just
    to render ``--help``. This subclass defers each subcommand import
    until Click actually needs to resolve it.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:  # noqa: D401
        return list(_SUBCOMMANDS)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        module_path = _SUBCOMMANDS.get(cmd_name)
        if module_path is None:
            return None
        module = importlib.import_module(module_path)
        cmd = getattr(module, "command", None)
        if cmd is None:
            raise click.ClickException(
                f"Subcommand module {module_path!r} is missing a top-level"
                " 'command' Click object."
            )
        return cmd


@click.group(
    cls=LazyGroup,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, "-V", "--version", prog_name="hyperagent0")
@click.pass_context
def main(ctx: click.Context, **_: Any) -> None:
    """hyperagent0 — host-first agentic harness (alias: ``haz``).

    Run ``haz --help`` for the full subcommand list, or ``haz start`` to
    launch the daemon.
    """

    if ctx.invoked_subcommand is not None:
        return

    # D4: bare 'haz' == 'haz status' + hint. Never silently start.
    status_cmd = ctx.command.get_command(ctx, "status")  # type: ignore[attr-defined]
    if status_cmd is None:  # pragma: no cover - subcommand always registered
        click.echo("hyperagent0: status command unavailable", err=True)
        ctx.exit(1)
    # Invoke 'status' with no args; pass --json off by default.
    ctx.invoke(status_cmd)
    click.echo(
        "\nRun 'haz start' to launch the daemon, or 'haz --help' for all commands."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
