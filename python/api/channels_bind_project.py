"""POST /channels_bind_project — set or clear a channel→project binding (spec 08 1.12, D7).

Body: ``{"channel": "slack", "chat_id": "C123", "project": "work"}``.

* Omit ``chat_id`` (or pass ``null`` / empty) to write the
  per-channel ``"default"`` mapping that catches every chat.
* Pass ``project: null`` (or omit it) to clear the entry instead of
  setting it.

The router reads ``channel_config.project_for_chat(...)`` per inbound
message, so changes are picked up live — no daemon restart needed.
That's why this endpoint is separate from ``/channels_apply``.
"""

from typing import Any

from python.helpers.api import ApiHandler, Request, Response


class ChannelsBindProject(ApiHandler):
    async def process(
        self, input: dict[str, Any], request: Request
    ) -> dict[str, Any] | Response:
        from hyperagent0.channels.channels_config_bridge import (
            FileChannelsConfigBridge,
        )

        channel_type = str(input.get("channel", "")).strip()
        if not channel_type:
            return {"success": False, "error": "channel is required"}

        # Empty string == "default". The bridge treats None as default
        # too, so we normalize empty/missing → None here.
        raw_chat_id = input.get("chat_id")
        chat_id = str(raw_chat_id).strip() if raw_chat_id else None
        chat_id = chat_id or None  # collapse empty string

        raw_project = input.get("project")
        project = str(raw_project).strip() if raw_project else None
        project = project or None

        bridge = FileChannelsConfigBridge()
        bridge.update_project_binding(
            channel_type, chat_id=chat_id, project_name=project
        )

        return {
            "success": True,
            "channel": channel_type,
            "project_binding": dict(
                bridge.read_block(channel_type).get("project_binding", {})
            ),
        }
