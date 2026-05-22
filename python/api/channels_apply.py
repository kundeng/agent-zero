"""POST /channels_apply — restart channel adapters (spec 08 1.11).

Idempotent. After a provisioner has written new tokens / config
blocks, the daemon needs to:

1. Disconnect every adapter that was running with the old config.
2. Re-read ``~/.hyperagent0/channels.json``.
3. Connect every adapter whose ``enabled: true``.

That's exactly what :func:`hyperagent0.channels.lifecycle.restart_channels`
does. The UI's "Apply" button hits this endpoint; the CLI's
``haz channel apply`` does the same.

Returns the post-restart state so the UI doesn't need to follow up
with a separate ``/channels_status`` call.
"""

from typing import Any

from python.helpers.api import ApiHandler, Request, Response


class ChannelsApply(ApiHandler):
    async def process(
        self, input: dict[str, Any], request: Request
    ) -> dict[str, Any] | Response:
        from hyperagent0.channels.lifecycle import restart_channels, running_adapters

        # Run synchronously — restart_channels has its own internal lock
        # and the disconnect/connect futures resolve on the channels
        # event loop, not this thread.
        restart_channels()

        return {
            "success": True,
            "live": running_adapters(),
        }
