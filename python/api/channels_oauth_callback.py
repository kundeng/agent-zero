"""GET /channels_oauth_callback — OAuth redirect target (spec 08 1.10 + D4).

Slack (and any future OAuth-based platform) redirects the user's
browser to this URL after they click "Allow" on the install consent
page. The URL pattern is:

    http(s)://<haz-host>:<port>/channels_oauth_callback
        ?channel_type=slack&code=...&state=...&session_id=...

We dispatch on the ``channel_type`` query parameter rather than a
path segment (e.g. ``/oauth/slack/callback``) because Agent Zero's
upstream API auto-loader at ``run_ui.py:494`` registers each
handler under ``/<module_name>`` — adding path variables would
require an upstream patch. Slack's OAuth flow preserves query
parameters across the redirect, so the parameter-based routing is
behaviorally identical.

This handler is *not* CSRF-protected and does *not* require auth —
the redirect originates from Slack, which has neither cookie nor
CSRF token. Security relies on the ``state`` parameter (a per-
session token minted at provision-start and verified here via
:meth:`ProvisionContext.consume_state_token`).

The response is a small HTML page that posts the result back to
``window.opener`` (the open Channels tab in the original browser
window) and closes itself. If the wizard tab is gone (user closed
it), the page still shows the result in a basic layout.
"""

import html
import json
from typing import Any

from python.helpers.api import ApiHandler, Request, Response


def _render_callback_page(result_json: dict[str, Any], session_id: str) -> str:
    """Return the small HTML page that closes the popup and notifies opener."""

    payload = {
        "type": "hyperagent0:channel-oauth-callback",
        "session_id": session_id,
        "result": result_json,
    }
    payload_js = json.dumps(payload)
    message = result_json.get("message") or "Provisioning continues in the app."
    error = result_json.get("error")

    body_html = (
        f'<p class="ok">{html.escape(message)}</p>'
        if not error
        else f'<p class="err">Error: {html.escape(str(error))}</p>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Channel install — HyperAgent Zero</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; padding: 2rem; max-width: 36rem; margin: 0 auto; }}
    .ok {{ color: #2a7a37; }}
    .err {{ color: #c0392b; }}
    .hint {{ color: #888; margin-top: 1rem; font-size: 0.9rem; }}
  </style>
</head>
<body>
  <h2>Channel install received</h2>
  {body_html}
  <p class="hint">You can close this tab. The Settings panel in your app
  has been notified.</p>
  <script>
    (function() {{
      var payload = {payload_js};
      try {{
        if (window.opener && !window.opener.closed) {{
          window.opener.postMessage(payload, "*");
          setTimeout(function() {{ window.close(); }}, 600);
        }}
      }} catch (e) {{ /* opener might be cross-origin; ignore */ }}
    }})();
  </script>
</body>
</html>"""


class ChannelsOauthCallback(ApiHandler):
    """OAuth redirect target — public, no auth, no CSRF."""

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["GET"]

    @classmethod
    def requires_auth(cls) -> bool:
        return False

    @classmethod
    def requires_csrf(cls) -> bool:
        return False

    async def process(
        self, input: dict[str, Any], request: Request
    ) -> dict[str, Any] | Response:
        from hyperagent0.channels.provision.dispatch import run_oauth_callback

        # Slack delivers parameters as the URL query string. We mirror
        # that for any future OAuth-based platform.
        query = {k: v for k, v in request.args.items()}
        channel_type = query.get("channel_type", "").strip()
        session_id = query.get("session_id") or None

        if not channel_type:
            return Response(
                response="Missing channel_type query parameter.",
                status=400,
                mimetype="text/plain",
            )

        host_base_url = request.host_url.rstrip("/")

        try:
            ctx, result = run_oauth_callback(
                channel_type,
                query,
                session_id=session_id,
                host_base_url=host_base_url,
            )
        except LookupError as exc:
            return Response(response=str(exc), status=404, mimetype="text/plain")

        body = _render_callback_page(result.to_json(), ctx.session_id)
        return Response(response=body, status=200, mimetype="text/html")
