"""Discord provisioner (spec 08 task 2.2).

Discord's "bot user" model: the user creates an application at
https://discord.com/developers/applications, attaches a bot to it,
copies the bot token, and then **separately** invites the bot to
their server using a URL composed from the application id and the
permissions the bot needs.

Two wizard steps:

1. **Paste bot token + application id**. We validate the token with
   ``GET /users/@me`` and store both values.
2. **Show invite URL**. The user clicks it to add the bot to their
   server. No callback — Discord doesn't redirect back to us; once
   the user has added the bot, the runtime adapter discovers it on
   its own when the daemon (re)starts.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .base import (
    BaseProvisioner,
    ProvisionContext,
    StepResult,
    WizardField,
    WizardStep,
    register_provisioner,
)

logger = logging.getLogger(__name__)


_DISCORD_API_BASE = "https://discord.com/api/v10"
_DISCORD_TIMEOUT_S = 10.0


# Conservative default permissions bitmask for an Agent-Zero-style bot.
# 0x800 = Send Messages, 0x10000 = Read Message History, 0x400 = View Channel.
# Bit math: 0x800 | 0x10000 | 0x400 = 0x10C00 = 68608.
_DEFAULT_PERMISSIONS = 68608

# Standard gateway intents for Message Content + Guilds + Guild Messages.
# Discord exposes these as ints; pre-computed sum kept here for clarity.
# scope=bot is what's needed for non-slash-command bots.
_DEFAULT_SCOPES = "bot"


# ---------------------------------------------------------------------------
# Tiny HTTP helper
# ---------------------------------------------------------------------------


def _discord_get(token: str, path: str) -> dict[str, Any]:
    """GET ``<api>/<path>`` with bot-token auth. Returns parsed JSON."""

    url = f"{_DISCORD_API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bot {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_DISCORD_TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(
            f"discord GET {path}: HTTP {exc.code} — {body_text[:200]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"discord GET {path}: network error — {exc.reason}"
        ) from exc

    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Provisioner
# ---------------------------------------------------------------------------


class DiscordProvisioner(BaseProvisioner):
    """Discord channel provisioner — paste-token plus invite-URL show."""

    channel_type = "discord"
    required_secrets = ["DISCORD_BOT_TOKEN", "DISCORD_APPLICATION_ID"]
    bootstrap_url = "https://discord.com/developers/applications"

    def wizard_steps(self) -> list[WizardStep]:
        return [
            WizardStep(
                id="credentials",
                kind="input",
                label="Paste your Discord bot token + application id",
                help_text=(
                    "From https://discord.com/developers/applications: "
                    "create a New Application, attach a Bot, copy the "
                    "bot token from the Bot page, and copy the "
                    "Application ID from the General Information page."
                ),
                fields=[
                    WizardField(
                        id="bot_name",
                        label="Bot name",
                        kind="text",
                        placeholder="default",
                        default="default",
                        required=True,
                        help_text=(
                            "Local identifier for this bot — used in "
                            "logs and per-bot secret keys. Pick a "
                            "unique name to run more than one Discord "
                            "bot from this install."
                        ),
                    ),
                    WizardField(
                        id="bot_token",
                        label="Bot token",
                        kind="password",
                        secret=True,
                        required=True,
                    ),
                    WizardField(
                        id="application_id",
                        label="Application ID",
                        kind="text",
                        required=True,
                        placeholder="123456789012345678",
                    ),
                ],
                next_on_success="invite",
            ),
            WizardStep(
                id="invite",
                kind="link_with_paste",
                label="Add the bot to your Discord server",
                help_text=(
                    "Open the URL below and pick a server. After you "
                    "confirm the permissions, the bot joins the "
                    "server. No further input needed here — just "
                    "confirm and continue."
                ),
                fields=[
                    WizardField(
                        id="confirm",
                        label="Type 'done' once you've added the bot",
                        kind="text",
                        required=True,
                        placeholder="done",
                    ),
                ],
                next_on_success="summary",
            ),
            WizardStep(
                id="summary",
                kind="summary",
                label="Ready to apply",
                help_text=(
                    "Discord bot configured. Click Apply to start "
                    "the channel adapter."
                ),
            ),
        ]

    def provision(
        self, step_id: str, inputs: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        if step_id == "credentials":
            return self._step_credentials(inputs, ctx)
        if step_id == "invite":
            return self._step_invite(inputs, ctx)
        if step_id == "summary":
            return StepResult(
                terminal=True, message="Discord channel configured."
            )
        return StepResult(error=f"unknown step {step_id!r}")

    def _step_credentials(
        self, inputs: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        bot_token = str(inputs.get("bot_token", "")).strip()
        application_id = str(inputs.get("application_id", "")).strip()
        if not bot_token:
            return StepResult(
                error="bot_token is required", error_pointer="/bot_token"
            )
        if not application_id:
            return StepResult(
                error="application_id is required",
                error_pointer="/application_id",
            )
        if not application_id.isdigit():
            return StepResult(
                error="application_id must be numeric (a Discord snowflake)",
                error_pointer="/application_id",
            )

        # Validate the token.
        try:
            me = _discord_get(bot_token, "users/@me")
        except RuntimeError as exc:
            return StepResult(error=str(exc), error_pointer="/bot_token")

        bot_name = str(me.get("username", "")).strip()
        if not bot_name:
            return StepResult(
                error="Discord /users/@me missing username — token may be invalid",
                error_pointer="/bot_token",
            )

        from hyperagent0.channels.config import secret_key_for_bot

        ctx.secrets.write({
            secret_key_for_bot(ctx.bot_name, "DISCORD_BOT_TOKEN"): bot_token,
            secret_key_for_bot(ctx.bot_name, "DISCORD_APPLICATION_ID"): application_id,
        })
        ctx.session["bot_username"] = bot_name
        ctx.session["application_id"] = application_id

        invite_url = self._build_invite_url(application_id)
        return StepResult(
            next_step="invite",
            message=f"Connected as {bot_name}#{me.get('discriminator', '0')}.",
            url_override=invite_url,
            extra={"username": bot_name, "invite_url": invite_url},
        )

    def _step_invite(
        self, inputs: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        # We don't validate the 'confirm' input value beyond "non-empty".
        # The actual proof that the bot joined a server is the runtime
        # adapter seeing GuildCreate events — out of scope for the
        # provisioner.
        confirm = str(inputs.get("confirm", "")).strip()
        if not confirm:
            return StepResult(
                error="Type 'done' to confirm you've added the bot.",
                error_pointer="/confirm",
            )

        block = self.channels_json_block(ctx)
        if ctx.bot_name:
            ctx.channels_config.set_bot_block(
                self.channel_type, ctx.bot_name, block
            )
        else:
            ctx.channels_config.update_block(self.channel_type, block)

        return StepResult(
            next_step="summary",
            message="Bot invite confirmed. Ready to start the adapter.",
        )

    def oauth_callback(
        self, query: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        # Discord supports OAuth bot installs but we use the invite-URL
        # path which doesn't redirect back to us. Reserved for future
        # expansion.
        return StepResult(
            error="discord provisioner does not handle OAuth callbacks"
        )

    def test_connection(self, ctx: ProvisionContext) -> str:
        from hyperagent0.channels.config import secret_key_for_bot

        key = secret_key_for_bot(ctx.bot_name, "DISCORD_BOT_TOKEN")
        token = ctx.secrets.read(key)
        if not token:
            raise RuntimeError(
                f"{key} not configured; provision the channel first"
            )
        me = _discord_get(token, "users/@me")
        username = me.get("username", "?")
        disc = me.get("discriminator", "0")
        return f"Connected to Discord as {username}#{disc}."

    def channels_json_block(self, ctx: ProvisionContext) -> dict[str, Any]:
        from hyperagent0.channels.config import secret_key_for_bot

        if ctx.bot_name:
            existing = ctx.channels_config.read_bot_block(
                self.channel_type, ctx.bot_name
            )
        else:
            existing = ctx.channels_config.read_block(self.channel_type)

        token_key = secret_key_for_bot(ctx.bot_name, "DISCORD_BOT_TOKEN")
        block = dict(existing)
        block.update(
            {
                "enabled": True,
                "token": f"$$secret({token_key})",
                "application_id": ctx.session.get("application_id", "")
                or existing.get("application_id", ""),
            }
        )
        block.setdefault("require_mention", False)
        block.setdefault("project_binding", {})
        block.setdefault("allowed_users", [])
        block.setdefault("allowed_chats", [])
        return block

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_invite_url(application_id: str) -> str:
        return (
            "https://discord.com/oauth2/authorize?"
            + urllib.parse.urlencode(
                {
                    "client_id": application_id,
                    "scope": _DEFAULT_SCOPES,
                    "permissions": str(_DEFAULT_PERMISSIONS),
                }
            )
        )


register_provisioner(DiscordProvisioner.channel_type, DiscordProvisioner)
