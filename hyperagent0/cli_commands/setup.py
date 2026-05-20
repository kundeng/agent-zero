"""``haz setup`` — interactive first-run wizard.

Self-contained: reads and writes ``usr/settings.json`` directly via
stdlib ``json``. The upstream settings helper provides defaults at
runtime, so a partial settings file is sufficient — we only need to
persist the fields the user explicitly changes here. This avoids
pulling LiteLLM / models.py / browser_use through the cold path.
"""

from __future__ import annotations

import json
from pathlib import Path

import click


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _settings_path() -> Path:
    return _repo_root() / "usr" / "settings.json"


def _load_settings() -> dict:
    path = _settings_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"failed to read {path}: {exc}")


def _save_settings(data: dict) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@click.command("setup")
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="Print the settings file path and exit without prompting.",
)
def command(non_interactive: bool) -> None:
    """Interactive first-run wizard for the most common settings."""

    settings_path = _settings_path()
    current = _load_settings()

    if non_interactive:
        click.echo(f"hyperagent0 settings file: {settings_path}")
        return

    click.echo("hyperagent0 setup wizard")
    click.echo(f"Settings file: {settings_path}")
    click.echo("Press Enter to keep the current value.\n")

    provider = click.prompt(
        "Chat model provider",
        default=current.get("chat_model_provider", "openai"),
    )
    model = click.prompt(
        "Chat model name",
        default=current.get("chat_model_name", "claude-sonnet-4-5"),
    )
    api_base = click.prompt(
        "Chat model API base (blank for provider default)",
        default=current.get("chat_model_api_base", "") or "",
        show_default=True,
    )
    sandbox_mode = click.prompt(
        "Sandbox mode (none / sandbox / ssh / cgroup / docker / podman)",
        default=current.get("sandbox_mode", "none"),
    )

    new = dict(current)
    new["chat_model_provider"] = provider
    new["chat_model_name"] = model
    if api_base:
        new["chat_model_api_base"] = api_base
    elif "chat_model_api_base" in new and new["chat_model_api_base"] == "":
        # Clear empty placeholders.
        del new["chat_model_api_base"]
    new["sandbox_mode"] = sandbox_mode

    _save_settings(new)
    click.echo(f"\nSettings saved to {settings_path}.")
    click.echo("Run 'haz start' to launch the daemon.")
