"""``haz check`` — verify the configured LLM is actually reachable.

Closes the "is my install actually working?" loop. Reads
``usr/settings.json`` (without booting the daemon) and makes a minimal
LiteLLM round trip. Prints OK + latency on success, or a one-line
diagnosis + nonzero exit on failure.

Exit codes (so wrapper scripts can branch):

  0  reachable + response received
  1  no settings file (LLM not configured yet)
  2  required field missing in settings
  3  network/auth/quota failure when calling the provider
  4  provider type not supported by this check (e.g. claude-sdk)

Heavy imports (``litellm``) are deferred to the command body so the
spec 03 D5 cold-start budget for ``haz --help`` / ``haz status`` is
unaffected. ``--help`` for this command only imports ``click``.
"""

from __future__ import annotations

import json
import sys
import time

import click

from .. import paths as _paths


def _load_settings() -> dict | None:
    path = _paths.settings_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"failed to read {path}: {exc}")


@click.command("check")
@click.option(
    "--model",
    default=None,
    help="Override chat_model_name from settings.json for this check.",
)
@click.option(
    "--api-base",
    default=None,
    help="Override chat_model_api_base for this check.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Print the provider's reply and per-step latencies.",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Hard limit on the round-trip in seconds.",
)
def command(
    model: str | None,
    api_base: str | None,
    verbose: bool,
    timeout: float,
) -> None:
    """Ping the configured LLM and report whether it answers."""

    settings = _load_settings()
    if settings is None:
        click.echo(
            f"no settings file at {_paths.settings_path()}. "
            "Run 'haz start' and configure the LLM in the web UI's Settings panel first.",
            err=True,
        )
        sys.exit(1)

    provider = settings.get("chat_model_provider")
    name = model or settings.get("chat_model_name")
    base = api_base if api_base is not None else settings.get("chat_model_api_base", "")

    if not provider:
        click.echo("chat_model_provider is not set in settings.json.", err=True)
        sys.exit(2)
    if not name:
        click.echo("chat_model_name is not set in settings.json.", err=True)
        sys.exit(2)

    if provider == "claude-sdk":
        click.echo(
            "claude-sdk provider bypasses LiteLLM; CLI check not implemented yet. "
            "Test it from the web UI's Settings panel.",
            err=True,
        )
        sys.exit(4)

    if verbose:
        click.echo(f"  provider:      {provider}")
        click.echo(f"  model:         {name}")
        click.echo(f"  api_base:      {base or '(provider default)'}")
        click.echo(f"  timeout:       {timeout}s")

    # Heavy import deferred until we actually need it.
    try:
        from litellm import completion
    except ImportError as exc:  # pragma: no cover
        click.echo(
            f"litellm not installed: {exc}. Re-run install.sh or 'pip install litellm'.",
            err=True,
        )
        sys.exit(3)

    # LiteLLM picks the wire protocol from the model name prefix. The
    # upstream settings convention is "provider" + "model" as separate
    # fields; for non-openai providers we may need to prefix.
    model_arg = name
    if provider != "openai" and "/" not in name:
        # e.g. provider="anthropic", name="claude-sonnet-4-5" -> "anthropic/claude-sonnet-4-5"
        model_arg = f"{provider}/{name}"

    kwargs: dict = {
        "model": model_arg,
        "messages": [{"role": "user", "content": "Say 'ok' and nothing else."}],
        "max_tokens": 8,
        "timeout": timeout,
    }
    if base:
        kwargs["api_base"] = base

    started = time.monotonic()
    try:
        # Synchronous call — keeps the check command simple. The real
        # daemon uses acompletion, but a one-shot diagnostic doesn't
        # need an event loop.
        response = completion(**kwargs)
    except Exception as exc:
        elapsed = time.monotonic() - started
        msg = str(exc)
        first_line = msg.splitlines()[0][:200] if msg else exc.__class__.__name__
        click.echo(f"FAIL ({elapsed:.2f}s): {first_line}", err=True)
        if verbose:
            click.echo(msg, err=True)
        sys.exit(3)

    elapsed = time.monotonic() - started
    try:
        content = response["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        content = "<could not parse response>"

    if verbose:
        click.echo(f"  reply:         {content.strip()!r}")
        click.echo(f"  latency:       {elapsed:.2f}s")

    click.echo(f"OK ({elapsed:.2f}s) - {provider}/{name} responded.")
