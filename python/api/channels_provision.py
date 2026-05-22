"""POST /channels_provision — dispatch one wizard step (spec 08 1.9).

Body shape: ``{"channel_type": "slack", "step_id": "config_token",
"inputs": {...}, "session_id": "..."}``.

Returns the serialized :class:`StepResult` from the provisioner.
The UI advances the wizard based on ``next_step`` and ``terminal``.
``session_id`` is echoed back so subsequent steps can correlate
into the same multi-step session.

Slow HTTP calls inside provisioners happen on the same thread that
serves this request — that's fine for the daemon's event loop
because Flask handlers already run in a worker thread (the Starlette
WSGIMiddleware path in ``run_ui.py``). The agent loop is on a
different thread; provisioning never blocks it.
"""

from typing import Any

from python.helpers.api import ApiHandler, Request, Response


class ChannelsProvision(ApiHandler):
    async def process(
        self, input: dict[str, Any], request: Request
    ) -> dict[str, Any] | Response:
        from hyperagent0.channels.provision.dispatch import run_step

        channel_type = str(input.get("channel_type", "")).strip()
        step_id = str(input.get("step_id", "")).strip()
        inputs = input.get("inputs") or {}
        session_id = input.get("session_id") or None

        if not channel_type or not step_id:
            return {
                "success": False,
                "error": "channel_type and step_id are required",
            }

        # Daemon's externally-reachable base URL: provisioners use this
        # to compose redirect_url values (e.g. for Slack's OAuth flow).
        # Use request.host_url so we honor whatever ingress fronted us
        # — running behind a reverse proxy / Cloudflare tunnel just
        # works as long as the proxy passes the original Host header.
        host_base_url = request.host_url.rstrip("/")

        try:
            ctx, result = run_step(
                channel_type,
                step_id,
                inputs,
                session_id=session_id,
                host_base_url=host_base_url,
            )
        except LookupError as exc:
            return {"success": False, "error": str(exc)}

        return {
            "success": result.error is None,
            "session_id": ctx.session_id,
            "result": result.to_json(),
        }
