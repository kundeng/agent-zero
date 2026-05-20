"""``haz config`` — read/write daemon configuration.

Stub for P2/P3 work — currently only supports printing the active
settings path so users can edit the underlying JSON by hand. The full
``get`` / ``set`` / ``edit`` surface is tracked in spec 03 task 3.1.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click


def _settings_module():
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from python.helpers import settings as settings_helper  # type: ignore

    return settings_helper


@click.group("config")
def command() -> None:
    """Read or modify hyperagent0 configuration."""


@command.command("path")
def _path() -> None:
    """Print the path of the active settings file."""

    settings_helper = _settings_module()
    path = getattr(settings_helper, "SETTINGS_FILE", None) or getattr(
        settings_helper, "settings_file_path", None
    )
    click.echo(str(path) if path else "(unknown)")


@command.command("get")
@click.argument("key")
def _get(key: str) -> None:
    """Print the current value for ``KEY``."""

    settings_helper = _settings_module()
    current = settings_helper.get_settings()
    if key not in current:
        click.echo(f"hyperagent0: unknown setting {key!r}", err=True)
        raise click.exceptions.Exit(1)
    click.echo(current[key])
