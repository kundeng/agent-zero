"""``haz setup`` — optional CLI configuration helper.

Call patterns:

  haz setup                          # interactive prompts
  haz setup --sandbox docker         # write just sandbox_mode
  haz setup --provider openai --model X --api-base Y    # write LLM fields

This command is OPTIONAL. The web UI's Settings panel is the primary
configuration path, mirroring upstream agent-zero. install.sh does not
call this; new users go directly from ``haz start`` to the UI.

Non-interactive mode only writes the fields you pass as flags — no
opinionated defaults, no silent overwrites of unrelated keys.

Self-contained: reads/writes ``usr/settings.json`` via stdlib ``json``.
Pulling models.py / LiteLLM / browser_use through ``haz setup`` would
blow the spec 03 cold-start budget, so the upstream settings helper is
deliberately not imported here.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from .. import paths as _paths


# Defaults SHOWN in the interactive wizard (not silently written by
# non-interactive mode). Mirror the proxy LLM documented in CLAUDE.md so
# someone running ``haz setup`` and hitting Enter four times gets a
# working local config; users with their own setup type over them.
_INTERACTIVE_DEFAULTS: dict[str, str] = {
    "chat_model_provider": "openai",
    "chat_model_name": "cc/claude-sonnet-4-6",
    "chat_model_api_base": "http://localhost:20128",
    "sandbox_mode": "none",
}

_VALID_SANDBOX_MODES = ("none", "sandbox", "ssh", "cgroup", "docker", "podman")


def _settings_path() -> Path:
    return _paths.settings_path()


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
@click.option("--provider", type=str, default=None, help="chat_model_provider (e.g. openai, anthropic, claude-sdk)")
@click.option("--model", type=str, default=None, help="chat_model_name (e.g. cc/claude-sonnet-4-6)")
@click.option("--api-base", type=str, default=None, help="chat_model_api_base URL; pass empty string to clear")
@click.option(
    "--sandbox",
    type=click.Choice(_VALID_SANDBOX_MODES, case_sensitive=False),
    default=None,
    help="code-execution sandbox mode (spec 05)",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="Print the settings file path and exit; do not modify anything.",
)
def command(
    provider: str | None,
    model: str | None,
    api_base: str | None,
    sandbox: str | None,
    non_interactive: bool,
) -> None:
    """Configure HyperAgent Zero (optional — the web UI handles this too)."""

    settings_path = _settings_path()
    current = _load_settings()

    if non_interactive:
        click.echo(f"hyperagent0 settings file: {settings_path}")
        return

    # ----- flag-only path: write ONLY the fields the user passed. -----
    # No opinionated defaults, no silent additions to unrelated keys.
    flag_overrides = {
        "chat_model_provider": provider,
        "chat_model_name": model,
        "chat_model_api_base": api_base,
        "sandbox_mode": sandbox,
    }
    explicit_flags = {k: v for k, v in flag_overrides.items() if v is not None}

    if explicit_flags:
        new = dict(current)
        for key, value in explicit_flags.items():
            if key == "chat_model_api_base" and value == "":
                new.pop(key, None)
            else:
                new[key] = value
        _save_settings(new)
        for key in explicit_flags:
            click.echo(f"  {key} = {new.get(key, '<cleared>')}")
        click.echo(f"settings written to {settings_path}")
        return

    # ----- interactive path -----
    click.echo("hyperagent0 setup wizard")
    click.echo(f"Settings file: {settings_path}")
    click.echo("Press Enter to keep the current value.\n")

    provider_in = click.prompt(
        "Chat model provider",
        default=current.get("chat_model_provider", _INTERACTIVE_DEFAULTS["chat_model_provider"]),
    )
    model_in = click.prompt(
        "Chat model name",
        default=current.get("chat_model_name", _INTERACTIVE_DEFAULTS["chat_model_name"]),
    )
    api_base_in = click.prompt(
        "Chat model API base (blank for provider default)",
        default=current.get("chat_model_api_base", "") or "",
        show_default=True,
    )
    sandbox_in = click.prompt(
        f"Sandbox mode ({' / '.join(_VALID_SANDBOX_MODES)})",
        default=current.get("sandbox_mode", _INTERACTIVE_DEFAULTS["sandbox_mode"]),
    )

    new = dict(current)
    new["chat_model_provider"] = provider_in
    new["chat_model_name"] = model_in
    if api_base_in:
        new["chat_model_api_base"] = api_base_in
    elif "chat_model_api_base" in new and new["chat_model_api_base"] == "":
        del new["chat_model_api_base"]
    new["sandbox_mode"] = sandbox_in

    _save_settings(new)
    click.echo(f"\nSettings saved to {settings_path}.")
