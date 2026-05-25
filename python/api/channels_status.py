"""GET /channels_status — per-bot config + live-adapter dot (spec 09 task 1.12).

The response shape is the source of truth for the UI's Channels tab
status list. Spec 09 turned this into a per-bot feed: one row per
``(channel_type, bot_name)`` pair, plus a placeholder row when a
registered provisioner has no bots configured (so the UI still shows
"Slack (not configured)" alongside "Telegram (live)" with no
special-casing).

For each row:

* ``channel_type`` / ``bootstrap_url`` are shared across all bots of
  that platform; they come from the provisioner class registry.
* ``bot_name`` is the operator-chosen local id (``"default"`` for
  legacy single-bot installs migrated from spec 04's dict-shape
  ``channels.json``).
* ``required_secrets`` is per-bot: each provisioner-declared key gets
  suffixed with ``_<BOTNAME>`` so multiple bots on the same platform
  hold independent tokens. ``_legacy`` / ``default`` keep the bare
  keys (strangler-fig contract — see :func:`secret_key_for_bot`).
* ``configured`` is True iff every per-bot secret resolves to a
  non-empty value.
* ``live`` honors the dual key ``running_adapters()`` returns: bare
  ``"slack"`` for ``_legacy`` adapters, ``"slack/botname"`` otherwise.

Tokens never appear in the response. The UI shows the secret-key
names and a boolean "configured" — the actual values stay on disk.
"""

from typing import Any

from python.helpers.api import ApiHandler, Request, Response


class ChannelsStatus(ApiHandler):
    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET", "POST"]

    async def process(
        self, input: dict[str, Any], request: Request
    ) -> dict[str, Any] | Response:
        # Lazy imports so this file's module-level cost stays cheap
        # for the test collector (spec 03 D5).
        from hyperagent0.channels.config import (
            load_bot_configs,
            secret_key_for_bot,
        )
        from hyperagent0.channels.lifecycle import running_adapters
        from hyperagent0.channels.provision.dispatch import (
            ensure_provisioners_loaded,
            list_provisioners,
        )
        from hyperagent0.channels.secrets_bridge import AllowlistedSecretsBridge

        ensure_provisioners_loaded()
        bots_by_platform = load_bot_configs()
        live = running_adapters()

        channels: list[dict[str, Any]] = []
        for entry in list_provisioners():
            channel_type = entry["channel_type"]
            bootstrap_url = entry["bootstrap_url"]
            provisioner_secrets = list(entry["required_secrets"])
            bots = bots_by_platform.get(channel_type, [])

            if not bots:
                # Platform has a provisioner but no bots in channels.json.
                # Emit one placeholder so the UI can show "(not configured)"
                # with a "Provision" CTA. ``bot_name=""`` flags this row
                # as a placeholder rather than a real bot card.
                channels.append(
                    _placeholder_row(
                        channel_type=channel_type,
                        bootstrap_url=bootstrap_url,
                        provisioner_secrets=provisioner_secrets,
                    )
                )
                continue

            for bot in bots:
                per_bot_secret_keys = [
                    secret_key_for_bot(bot.bot_name, key)
                    for key in provisioner_secrets
                ]
                # Reads bypass the allow-list, so a single bridge can
                # serve every key regardless of which provisioner owns it.
                reader = AllowlistedSecretsBridge(per_bot_secret_keys)
                configured_secrets = {
                    key: bool(reader.read(key)) for key in per_bot_secret_keys
                }
                fully_configured = (
                    all(configured_secrets.values()) if configured_secrets else False
                )

                channels.append(
                    {
                        "channel_type": channel_type,
                        "bot_name": bot.bot_name,
                        "bootstrap_url": bootstrap_url,
                        "required_secrets": per_bot_secret_keys,
                        "configured_secrets": configured_secrets,
                        "configured": fully_configured,
                        "enabled": bool(bot.enabled),
                        "require_mention": bool(bot.require_mention),
                        "default_project": bot.default_project,
                        "project_overrides": dict(bot.project_overrides),
                        # Legacy field — keep populated so the existing
                        # bindProject() UI path keeps working on single-bot
                        # installs. Composite of default_project + per-chat.
                        "project_binding": _legacy_project_binding(bot),
                        "allowed_users": list(bot.allowed_users),
                        "allowed_chats": list(bot.allowed_chats),
                        "live": _is_live(live, channel_type, bot.bot_name),
                    }
                )

        return {"success": True, "channels": channels}


def _placeholder_row(
    *,
    channel_type: str,
    bootstrap_url: str,
    provisioner_secrets: list[str],
) -> dict[str, Any]:
    """Build the "(not configured)" row shown when a platform has no bots."""

    from hyperagent0.channels.secrets_bridge import AllowlistedSecretsBridge

    reader = AllowlistedSecretsBridge(provisioner_secrets)
    configured_secrets = {key: bool(reader.read(key)) for key in provisioner_secrets}
    return {
        "channel_type": channel_type,
        # Empty bot_name distinguishes placeholder rows from real bots.
        # The UI hides the suffix and offers a "Provision" CTA.
        "bot_name": "",
        "bootstrap_url": bootstrap_url,
        "required_secrets": provisioner_secrets,
        "configured_secrets": configured_secrets,
        "configured": False,
        "enabled": False,
        "require_mention": False,
        "default_project": "",
        "project_overrides": {},
        "project_binding": {},
        "allowed_users": [],
        "allowed_chats": [],
        "live": False,
    }


def _is_live(
    live_map: dict[str, dict[str, Any]],
    channel_type: str,
    bot_name: str,
) -> bool:
    """Honor both ``running_adapters()`` keying conventions.

    Spec 09 D5: ``_legacy`` adapters keep the bare ``channel_type`` key
    so existing status consumers stay correct; named bots use
    ``channel_type/bot_name``. We check both so a freshly migrated
    install (bot_name="default") still reports live when its adapter
    is up under either label.
    """

    composite = f"{channel_type}/{bot_name}"
    if live_map.get(composite, {}).get("live"):
        return True
    # Fall back to the bare key only for the strangler-fig case.
    if bot_name in ("_legacy", "default"):
        return bool(live_map.get(channel_type, {}).get("live"))
    return False


def _legacy_project_binding(bot) -> dict[str, str]:
    """Synthesize the spec-08 ``project_binding`` map from spec-09 fields.

    Spec 08's UI store keys binding edits off this map's ``default``
    entry. Until the store learns about ``default_project`` + per-bot
    overrides, we emit a composite so the existing flow keeps working.
    """

    binding: dict[str, str] = {}
    if bot.default_project:
        binding["default"] = bot.default_project
    binding.update({str(k): str(v) for k, v in bot.project_overrides.items()})
    return binding
