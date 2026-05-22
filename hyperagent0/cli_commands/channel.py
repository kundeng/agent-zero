"""``haz channel`` — thin CLI shim around the provisioning framework (spec 08 D8).

The CLI introspects each provisioner's :meth:`wizard_steps` to derive
its flag set. That means adding a new platform doesn't add a single
line of code to this file — the new ``--<field-id>`` options appear
automatically when the platform's wizard step descriptors land.

Three top-level subcommands:

* ``haz channel list`` — show every registered provisioner, its
  bootstrap URL, and the secrets it needs.
* ``haz channel status`` — current live/configured/enabled state per
  platform. Mirrors what the Settings UI shows.
* ``haz channel provision <platform> [--input KEY=VAL ...]`` — drive
  the wizard. For multi-step flows, the CLI walks every step that
  accepts inputs and either reads the values from ``--input`` flags
  or prompts interactively when stdin is a TTY.
* ``haz channel apply`` — restart the channel adapters in the daemon.

All work runs in-process via :mod:`hyperagent0.channels.provision.dispatch`
— there is no daemon-running vs daemon-not-running split (yet). A
future enhancement could POST to ``localhost:<port>/channels_*``
when the daemon is up so state stays in one place; for now the CLI
writes secrets and channels.json directly and tells the user to
``haz restart`` when they're done.

Heavy imports are deferred inside command bodies to keep
``haz --help`` and ``haz channel --help`` under the spec 03 D5
cold-start budget.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

import click


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group("channel", help="Manage chat channels (Slack, Telegram, Discord, …).")
def command() -> None:
    """``haz channel`` — provision and inspect chat-channel adapters."""


# ---------------------------------------------------------------------------
# `haz channel list`
# ---------------------------------------------------------------------------


@command.command("list")
def cmd_list() -> None:
    """List every registered provisioner."""

    from hyperagent0.channels.provision.dispatch import list_provisioners

    rows = list_provisioners()
    if not rows:
        click.echo("No provisioners registered.")
        return
    for row in rows:
        click.echo(f"  {row['channel_type']}")
        if row.get("bootstrap_url"):
            click.echo(f"    bootstrap: {row['bootstrap_url']}")
        secrets = row.get("required_secrets") or []
        if secrets:
            click.echo(f"    secrets:   {', '.join(secrets)}")


# ---------------------------------------------------------------------------
# `haz channel status`
# ---------------------------------------------------------------------------


@command.command("status")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def cmd_status(as_json: bool) -> None:
    """Show per-channel configured/enabled/live state."""

    from hyperagent0.channels.channels_config_bridge import FileChannelsConfigBridge
    from hyperagent0.channels.lifecycle import running_adapters
    from hyperagent0.channels.provision.dispatch import (
        ensure_provisioners_loaded,
        list_provisioners,
    )
    from hyperagent0.channels.secrets_bridge import AllowlistedSecretsBridge

    ensure_provisioners_loaded()
    bridge = FileChannelsConfigBridge()
    live = running_adapters()
    rows: list[dict[str, Any]] = []
    for entry in list_provisioners():
        ct = entry["channel_type"]
        block = bridge.read_block(ct)
        reader = AllowlistedSecretsBridge(entry["required_secrets"])
        configured_secrets = {
            k: bool(reader.read(k)) for k in entry["required_secrets"]
        }
        rows.append(
            {
                "channel_type": ct,
                "enabled": bool(block.get("enabled", False)),
                "configured": all(configured_secrets.values()) if configured_secrets else False,
                "live": bool(live.get(ct, {}).get("live", False)),
                "configured_secrets": configured_secrets,
            }
        )

    if as_json:
        click.echo(json.dumps({"channels": rows}, indent=2))
        return

    if not rows:
        click.echo("No provisioners registered.")
        return
    for r in rows:
        flags = []
        if r["live"]:
            flags.append("live")
        elif r["enabled"]:
            flags.append("enabled (not running)")
        elif r["configured"]:
            flags.append("configured")
        else:
            flags.append("not configured")
        click.echo(f"  {r['channel_type']:10}  {', '.join(flags)}")


# ---------------------------------------------------------------------------
# `haz channel provision <platform>` — wizard-driven
# ---------------------------------------------------------------------------


@command.command(
    "provision",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("platform")
@click.option(
    "--input",
    "raw_inputs",
    multiple=True,
    metavar="KEY=VALUE",
    help=(
        "Wizard field input as KEY=VALUE. Use multiple times for multi-field steps. "
        "Field IDs come from the provisioner — see "
        "`haz channel provision <platform> --show-steps`."
    ),
)
@click.option(
    "--show-steps",
    is_flag=True,
    help="Print the platform's wizard steps and exit.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help=(
        "Never prompt for input — read all values from --input flags. "
        "Fail if any required field is missing."
    ),
)
def cmd_provision(
    platform: str,
    raw_inputs: tuple[str, ...],
    show_steps: bool,
    non_interactive: bool,
) -> None:
    """Run the wizard for ``platform`` (slack/telegram/discord/…).

    Without ``--show-steps`` and without enough ``--input`` flags, the
    command prompts for missing required fields when stdin is a TTY.
    Use ``--non-interactive`` in scripts where you've supplied
    everything upfront.
    """

    from hyperagent0.channels.provision.dispatch import (
        ensure_provisioners_loaded,
        get_provisioner_instance,
        run_step,
    )

    ensure_provisioners_loaded()

    try:
        provisioner = get_provisioner_instance(platform)
    except LookupError as exc:
        raise click.ClickException(str(exc))

    steps = provisioner.wizard_steps()

    if show_steps:
        for step in steps:
            click.echo(f"step: {step.id}  ({step.kind})")
            click.echo(f"  label: {step.label}")
            if step.help_text:
                click.echo(f"  help:  {step.help_text}")
            for f in step.fields:
                req = " (required)" if f.required else ""
                sec = " [secret]" if f.secret else ""
                click.echo(f"  field: --input {f.id}=<{f.kind}>{req}{sec}")
        return

    # Parse --input KEY=VALUE flags into a dict.
    user_inputs: dict[str, str] = {}
    for raw in raw_inputs:
        if "=" not in raw:
            raise click.ClickException(f"--input {raw!r} must be KEY=VALUE")
        key, value = raw.split("=", 1)
        user_inputs[key.strip()] = value

    session_id: Optional[str] = None
    current_idx = 0
    while current_idx < len(steps):
        step = steps[current_idx]

        # Skip steps that have no inputs to collect (link_with_callback,
        # info, summary). The user can't do anything from a CLI for
        # those except hit them, ack the message, and move on.
        if step.kind in ("link_with_callback", "info", "summary"):
            _print_step_header(step)
            if step.kind == "link_with_callback":
                # We don't have a URL until step 1 returns it. Hand
                # the user the URL we have if any.
                click.echo(
                    "  This step requires a browser to complete OAuth. "
                    "Run the wizard from the Web UI, or paste the bot "
                    "token via the fallback step."
                )
            inputs: dict[str, str] = {}
        elif step.kind in ("input", "link_with_paste"):
            inputs = _collect_inputs_for_step(step, user_inputs, non_interactive)
        else:
            inputs = {}

        ctx, result = run_step(
            platform, step.id, inputs, session_id=session_id
        )
        session_id = ctx.session_id

        if result.error:
            click.echo(f"  ERROR: {result.error}", err=True)
            sys.exit(1)
        if result.message:
            click.echo(f"  {result.message}")
        if result.url_override:
            click.echo(f"  Open this URL to install: {result.url_override}")
        if result.terminal:
            click.echo("Provisioning complete.")
            return

        if result.next_step:
            new_idx = _find_step_idx(steps, result.next_step)
            if new_idx < 0:
                raise click.ClickException(
                    f"provisioner returned unknown next_step={result.next_step!r}"
                )
            current_idx = new_idx
        else:
            current_idx += 1


# ---------------------------------------------------------------------------
# `haz channel apply`
# ---------------------------------------------------------------------------


@command.command("apply")
def cmd_apply() -> None:
    """Restart the channel adapters in the running daemon.

    Hits the same code path as ``/channels_apply`` in the Web UI. If
    no daemon is running, this is a no-op (the next ``haz start``
    will pick up whatever's currently in ``channels.json``).
    """

    from hyperagent0.channels.lifecycle import restart_channels

    restart_channels()
    click.echo("Channel adapters restarted (or were already idle).")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_step_header(step: Any) -> None:
    click.echo(f"--- step: {step.label} ---")
    if step.help_text:
        click.echo(f"  {step.help_text}")


def _find_step_idx(steps: list, step_id: str) -> int:
    for i, s in enumerate(steps):
        if s.id == step_id:
            return i
    return -1


def _collect_inputs_for_step(
    step: Any, user_inputs: dict[str, str], non_interactive: bool
) -> dict[str, str]:
    """Gather field values for ``step`` from flags + prompts.

    Required fields not in ``user_inputs`` either prompt (TTY) or
    raise (non-interactive / no TTY).
    """

    _print_step_header(step)
    out: dict[str, str] = {}
    for field in step.fields:
        if field.id in user_inputs:
            out[field.id] = user_inputs[field.id]
            continue
        if field.default is not None:
            out[field.id] = field.default
            # The user can still override with --input.
            continue
        if non_interactive:
            if field.required:
                raise click.ClickException(
                    f"missing required field --input {field.id}=..."
                )
            continue
        if not sys.stdin.isatty():
            if field.required:
                raise click.ClickException(
                    f"stdin is not a TTY; pass --input {field.id}=..."
                )
            continue
        prompt = field.label or field.id
        out[field.id] = click.prompt(
            prompt, hide_input=field.secret, default="" if not field.required else None
        )
    return out
