"""``haz setup`` — interactive first-run wizard.

Minimal v1 implementation: prompts for the values most users need to
change (API key, model, port) and writes them into the existing
upstream settings file. This keeps the wizard useful without dragging
in the full settings schema at CLI cold-start time.

Heavy imports (``python.helpers.settings``) happen inside
:func:`command` so we don't break the cold-start budget for anyone
else.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click


def _settings_module():
    """Lazy import of the upstream settings helper.

    The CLI is shipped as the ``hyperagent0`` wrapper package alongside
    the upstream ``python/`` tree; we have to make the repo root
    importable when invoked as a console_script.
    """

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from python.helpers import settings as settings_helper  # type: ignore

    return settings_helper


@click.command("setup")
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="Print the settings file path and exit without prompting.",
)
def command(non_interactive: bool) -> None:
    """Interactive first-run wizard for the most common settings."""

    settings_helper = _settings_module()
    current = settings_helper.get_settings()

    if non_interactive:
        # Best-effort surface where the settings file lives. Different
        # upstream versions expose this differently; fall back gracefully.
        path = getattr(settings_helper, "SETTINGS_FILE", None) or getattr(
            settings_helper, "settings_file_path", None
        )
        click.echo(f"hyperagent0 settings file: {path or '(unknown)'}")
        return

    click.echo("hyperagent0 setup wizard")
    click.echo("Press Enter to keep the current value.\n")

    provider = click.prompt(
        "Chat model provider",
        default=current.get("chat_model_provider", "openai"),
    )
    model = click.prompt(
        "Chat model name",
        default=current.get("chat_model_name", "claude-sonnet-4-20250514"),
    )
    api_base = click.prompt(
        "Chat model API base (blank for provider default)",
        default=current.get("chat_model_api_base", "") or "",
        show_default=True,
    )

    new = dict(current)
    new["chat_model_provider"] = provider
    new["chat_model_name"] = model
    if api_base:
        new["chat_model_api_base"] = api_base

    try:
        settings_helper.set_settings(new)  # type: ignore[attr-defined]
    except AttributeError:
        # Older upstream API; try the alternative name.
        try:
            settings_helper.save_settings(new)  # type: ignore[attr-defined]
        except AttributeError:
            click.echo(
                "hyperagent0: settings module lacks a known setter; aborting.",
                err=True,
            )
            raise click.exceptions.Exit(1)

    click.echo("\nSettings saved. Run 'haz start' to launch the daemon.")
