"""Telegram provisioner (spec 08 task 2.1).

End-to-end Telegram channel provisioning. Far simpler than Slack
because Telegram doesn't have a manifest API or OAuth flow — the
user creates a bot via BotFather (https://t.me/BotFather), copies
the bot token, and pastes it here.

One wizard step, two API calls:

1. ``getMe`` to validate the token and pull the bot's username.
2. (Optional) ``setMyCommands`` if the user supplies commands.

That's the whole thing.
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


_TG_API_BASE = "https://api.telegram.org"
_TG_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# HTTP wrappers (kept inside the provisioner module — too small to split)
# ---------------------------------------------------------------------------


def _tg_call(token: str, method: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Call ``api.telegram.org/bot<token>/<method>`` with optional JSON body.

    Returns the parsed response. Raises :class:`RuntimeError` on
    non-2xx or ``ok: false``.
    """

    url = f"{_TG_API_BASE}/bot{urllib.parse.quote(token, safe='')}/{method}"
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TG_TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(
            f"telegram {method}: HTTP {exc.code} — {body_text[:200]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"telegram {method}: network error — {exc.reason}"
        ) from exc

    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("ok"):
        desc = payload.get("description", "unknown error") if isinstance(payload, dict) else "invalid response"
        raise RuntimeError(f"telegram {method}: {desc}")
    return payload


# ---------------------------------------------------------------------------
# Provisioner
# ---------------------------------------------------------------------------


class TelegramProvisioner(BaseProvisioner):
    """Single-step Telegram provisioner."""

    channel_type = "telegram"
    required_secrets = ["TELEGRAM_BOT_TOKEN"]
    bootstrap_url = "https://t.me/BotFather"

    def wizard_steps(self) -> list[WizardStep]:
        return [
            WizardStep(
                id="bot_token",
                kind="input",
                label="Paste your Telegram bot token",
                help_text=(
                    "Open https://t.me/BotFather, run /newbot (or "
                    "/mybots if you already have one), and copy the "
                    "token. Format: 123456:ABC-DEF…"
                ),
                fields=[
                    WizardField(
                        id="bot_token",
                        label="Bot token",
                        kind="password",
                        secret=True,
                        required=True,
                        placeholder="123456:ABC-DEF…",
                    ),
                    WizardField(
                        id="allowed_users",
                        label="Allowed user IDs (comma-separated, optional)",
                        kind="text",
                        required=False,
                        placeholder="12345,67890",
                        help_text=(
                            "If set, only these user IDs can talk to "
                            "the bot. Leave blank for open access."
                        ),
                    ),
                    WizardField(
                        id="commands",
                        label="Bot command list (optional)",
                        kind="textarea",
                        required=False,
                        placeholder="start: Greet the user\nhelp: Show help",
                        help_text=(
                            "One 'cmd: description' per line. Pushed "
                            "to Telegram via setMyCommands so users "
                            "see autocomplete suggestions."
                        ),
                    ),
                ],
                next_on_success="summary",
            ),
            WizardStep(
                id="summary",
                kind="summary",
                label="Ready to apply",
                help_text=(
                    "Bot token saved. Click Apply to start the "
                    "Telegram channel adapter."
                ),
            ),
        ]

    def provision(
        self, step_id: str, inputs: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        if step_id == "summary":
            return StepResult(
                terminal=True, message="Telegram channel configured."
            )
        if step_id != "bot_token":
            return StepResult(error=f"unknown step {step_id!r}")

        bot_token = str(inputs.get("bot_token", "")).strip()
        if not bot_token:
            return StepResult(
                error="bot_token is required", error_pointer="/bot_token"
            )

        # Validate by calling getMe.
        try:
            response = _tg_call(bot_token, "getMe")
        except RuntimeError as exc:
            return StepResult(error=str(exc), error_pointer="/bot_token")

        result = response.get("result") or {}
        username = str(result.get("username", "")).strip()
        if not username:
            return StepResult(
                error="getMe response missing username — token may be invalid",
                error_pointer="/bot_token",
            )

        # Optional: setMyCommands
        commands_raw = str(inputs.get("commands", "")).strip()
        if commands_raw:
            commands = _parse_commands(commands_raw)
            if commands:
                try:
                    _tg_call(bot_token, "setMyCommands", {"commands": commands})
                except RuntimeError:
                    # Non-fatal — token works, just commands didn't take.
                    logger.warning("setMyCommands failed (continuing anyway)")

        ctx.secrets.write({"TELEGRAM_BOT_TOKEN": bot_token})
        ctx.session["bot_username"] = username

        # Build channels.json block.
        allowed_users_raw = str(inputs.get("allowed_users", "")).strip()
        block = self.channels_json_block(ctx)
        if allowed_users_raw:
            block["allowed_users"] = [
                u.strip() for u in allowed_users_raw.split(",") if u.strip()
            ]
        ctx.channels_config.update_block(self.channel_type, block)

        return StepResult(
            next_step="summary",
            message=f"Connected as @{username}.",
            extra={"username": username},
        )

    def oauth_callback(
        self, query: dict[str, Any], ctx: ProvisionContext
    ) -> StepResult:
        # Telegram does not use browser OAuth — bot-token paste only.
        return StepResult(
            error="telegram provisioner does not use OAuth callbacks"
        )

    def test_connection(self, ctx: ProvisionContext) -> str:
        token = ctx.secrets.read("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN not configured; provision the channel first"
            )
        response = _tg_call(token, "getMe")
        result = response.get("result") or {}
        username = result.get("username", "?")
        return f"Connected to Telegram as @{username}."

    def channels_json_block(self, ctx: ProvisionContext) -> dict[str, Any]:
        existing = ctx.channels_config.read_block(self.channel_type)
        block = dict(existing)
        block.update(
            {
                "enabled": True,
                "token": "$$secret(TELEGRAM_BOT_TOKEN)",
            }
        )
        block.setdefault("require_mention", False)
        block.setdefault("project_binding", {})
        block.setdefault("allowed_users", [])
        block.setdefault("allowed_chats", [])
        return block


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_commands(raw: str) -> list[dict[str, str]]:
    """Parse a 'cmd: description' block into Telegram's BotCommand format.

    Lines without a colon are skipped. Empty lines are ignored.
    """

    out: list[dict[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        cmd, _, desc = line.partition(":")
        cmd = cmd.strip().lstrip("/")
        desc = desc.strip()
        if cmd and desc:
            out.append({"command": cmd, "description": desc})
    return out


register_provisioner(TelegramProvisioner.channel_type, TelegramProvisioner)
