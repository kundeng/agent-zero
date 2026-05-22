"""GET /channels_status — per-channel config + live-adapter dot (spec 08 1.7).

The response shape is the source of truth for the UI's Channels tab
status list. One entry per registered provisioner, even when the
channel is not yet configured — that way the UI shows "Slack (not
configured)" alongside "Telegram (live)" with no special-casing.

For each entry:

* ``channel_type`` / ``bootstrap_url`` / ``required_secrets`` come
  from the provisioner class registry, so a new platform shows up
  the moment it's imported.
* ``enabled`` and ``configured`` reflect ``channels.json``:
  ``enabled`` is the on-disk flag; ``configured`` is True iff every
  ``required_secrets`` key resolves to a non-empty value in the
  secrets store.
* ``live`` reflects :func:`hyperagent0.channels.lifecycle.running_adapters`
  — present only when the daemon's channel runtime has the adapter
  connected.

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
        from hyperagent0.channels.channels_config_bridge import (
            FileChannelsConfigBridge,
        )
        from hyperagent0.channels.lifecycle import running_adapters
        from hyperagent0.channels.provision.dispatch import (
            ensure_provisioners_loaded,
            list_provisioners,
        )
        from hyperagent0.channels.secrets_bridge import AllowlistedSecretsBridge

        ensure_provisioners_loaded()

        config_bridge = FileChannelsConfigBridge()
        live = running_adapters()

        channels = []
        for entry in list_provisioners():
            channel_type = entry["channel_type"]
            block = config_bridge.read_block(channel_type)

            # "configured" = every declared secret has a value in usr/secrets.env.
            # The bridge's read() bypasses the allow-list so a status check
            # works even when the secret is read by code that didn't write it.
            secret_reader = AllowlistedSecretsBridge(entry["required_secrets"])
            configured_secrets = {
                key: bool(secret_reader.read(key))
                for key in entry["required_secrets"]
            }
            fully_configured = all(configured_secrets.values()) if configured_secrets else False

            channels.append(
                {
                    "channel_type": channel_type,
                    "bootstrap_url": entry["bootstrap_url"],
                    "required_secrets": entry["required_secrets"],
                    "configured_secrets": configured_secrets,
                    "configured": fully_configured,
                    "enabled": bool(block.get("enabled", False)),
                    "require_mention": bool(block.get("require_mention", False)),
                    "project_binding": dict(block.get("project_binding", {})),
                    "allowed_users": list(block.get("allowed_users", [])),
                    "allowed_chats": list(block.get("allowed_chats", [])),
                    "live": bool(live.get(channel_type, {}).get("live", False)),
                }
            )

        return {"success": True, "channels": channels}
