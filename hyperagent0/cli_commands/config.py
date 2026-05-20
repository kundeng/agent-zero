"""``haz config`` — read/write daemon configuration.

Self-contained: reads ``usr/settings.json`` directly via stdlib ``json``
to avoid pulling the full upstream model stack (LiteLLM, browser_use,
etc.) through ``python.helpers.settings``. Honors the cold-start
budget per spec 03 D5.
"""

from __future__ import annotations

import json
from pathlib import Path

import click


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _settings_path() -> Path:
    return _repo_root() / "usr" / "settings.json"


def _load_settings() -> dict | None:
    path = _settings_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"failed to read {path}: {exc}")


@click.group("config")
def command() -> None:
    """Read or modify hyperagent0 configuration."""


@command.command("path")
def _path() -> None:
    """Print the path of the active settings file."""

    click.echo(str(_settings_path()))


@command.command("get")
@click.argument("key")
def _get(key: str) -> None:
    """Print the current value for ``KEY``."""

    current = _load_settings()
    if current is None:
        click.echo(
            f"hyperagent0: no settings file at {_settings_path()}",
            err=True,
        )
        raise click.exceptions.Exit(1)
    if key not in current:
        click.echo(f"hyperagent0: unknown setting {key!r}", err=True)
        raise click.exceptions.Exit(1)
    value = current[key]
    if isinstance(value, (dict, list)):
        click.echo(json.dumps(value, indent=2))
    else:
        click.echo(value)
