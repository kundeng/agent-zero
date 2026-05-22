"""POST /channels_test — smoke-test a configured channel (spec 08 1.13).

Body: ``{"channel_type": "slack", "session_id": "..."}``.

Calls the provisioner's :meth:`BaseProvisioner.test_connection`,
which performs a platform-appropriate check (Slack: a single
``chat.postMessage`` to a "hyperagent0 channel test" line; Telegram:
``getMe``; Discord: ``/users/@me``).

Returns the human-facing message on success or surfaces the
exception text on failure. The UI shows the result inline on the
channel card.
"""

from typing import Any

from python.helpers.api import ApiHandler, Request, Response


class ChannelsTest(ApiHandler):
    async def process(
        self, input: dict[str, Any], request: Request
    ) -> dict[str, Any] | Response:
        from hyperagent0.channels.provision.dispatch import run_test_connection

        channel_type = str(input.get("channel_type", "")).strip()
        if not channel_type:
            return {"success": False, "error": "channel_type is required"}

        session_id = input.get("session_id") or None

        try:
            ctx, message = run_test_connection(channel_type, session_id=session_id)
        except LookupError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            # Provisioners raise on real failures (bad token, network
            # unreachable, etc.). Surface a short message; the daemon
            # log gets the full traceback.
            return {"success": False, "error": str(exc)}

        return {"success": True, "session_id": ctx.session_id, "message": message}
