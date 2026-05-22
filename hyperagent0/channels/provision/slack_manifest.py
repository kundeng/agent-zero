"""Slack app manifest builder (spec 08 D5, task 1.15).

Slack's `apps.manifest.create
<https://api.slack.com/methods/apps.manifest.create>`_ endpoint takes
a single workspace-level *configuration access token* and a JSON
manifest describing every aspect of the app the user wants to
register: display info, bot user, OAuth scopes, event subscriptions,
Socket Mode setting, redirect URLs, and more.

This module owns the manifest shape as a Python dict, NOT a YAML
template. Three reasons:

1. **No new runtime dependency.** A YAML parser would have to land in
   ``requirements.txt`` just to read a static 30-line file.
2. **Type errors caught at import time.** The function signature is
   the contract; misnames fail in tests rather than at the Slack API
   round-trip.
3. **Easy to mutate.** The wizard offers checkboxes for "support
   private channels" and "support DMs" — toggling those just augments
   ``scopes.bot`` and ``event_subscriptions.bot_events`` in code.

The manifest is converted to a *JSON-encoded string* when handed to
``apps.manifest.create`` — per the API contract, the request body's
``manifest`` field is a string, not a nested object.
"""

from __future__ import annotations

from typing import Any


# Base scopes — what the runtime adapter actually consumes.
# Trimmed deliberately to the minimum needed for the spec-04 Slack
# adapter to function. Optional scope groups are layered on top via
# ``include_private_channels`` / ``include_dms``.
_BASE_BOT_SCOPES: tuple[str, ...] = (
    "app_mentions:read",  # see ``@HyperAgent`` mentions
    "chat:write",  # post replies
    "channels:history",  # read messages in public channels we're invited to
    "channels:read",  # discover channels
)


_PRIVATE_CHANNEL_SCOPES: tuple[str, ...] = (
    "groups:history",
    "groups:read",
)


_DM_SCOPES: tuple[str, ...] = (
    "im:history",
    "im:write",
)


_BASE_BOT_EVENTS: tuple[str, ...] = (
    "app_mention",
    "message.channels",
)


_PRIVATE_CHANNEL_EVENTS: tuple[str, ...] = ("message.groups",)


_DM_EVENTS: tuple[str, ...] = ("message.im",)


def build_slack_manifest(
    display_name: str,
    redirect_url: str,
    *,
    app_name: str = "HyperAgent Zero",
    description: str = "HyperAgent Zero connected to Slack channels",
    include_private_channels: bool = True,
    include_dms: bool = True,
) -> dict[str, Any]:
    """Return the Slack-app manifest dict for one provisioning request.

    Parameters
    ----------
    display_name
        The ``bot_user.display_name`` shown in Slack ("agent-zero",
        "hyperagent", etc.). 1-21 chars, lowercase, no spaces — Slack
        will reject otherwise.
    redirect_url
        Full URL Slack should redirect to after the user clicks
        "Install to workspace". Must be reachable from the user's
        browser (see spec 08 Open Question on NAT'd hosts).
    app_name
        ``display_information.name``. Shown in the app catalog / install
        consent screen. Defaults to "HyperAgent Zero".
    description
        ``display_information.description``. Shown alongside the name.
    include_private_channels, include_dms
        Add the relevant scopes + ``bot_events`` to support those
        conversation types. The UI exposes both as checkboxes during
        provisioning; default-on so the most common Slack workspaces
        just work.

    Returns
    -------
    A dict ready to be ``json.dumps()``'d into the
    ``apps.manifest.create`` request body's ``manifest`` field.
    """

    scopes_bot = list(_BASE_BOT_SCOPES)
    bot_events = list(_BASE_BOT_EVENTS)

    if include_private_channels:
        scopes_bot.extend(_PRIVATE_CHANNEL_SCOPES)
        bot_events.extend(_PRIVATE_CHANNEL_EVENTS)

    if include_dms:
        scopes_bot.extend(_DM_SCOPES)
        bot_events.extend(_DM_EVENTS)

    return {
        "display_information": {
            "name": app_name,
            "description": description,
            "background_color": "#1a1a2e",
        },
        "features": {
            "bot_user": {
                "display_name": display_name,
                "always_online": True,
            },
        },
        "oauth_config": {
            "redirect_urls": [redirect_url],
            "scopes": {
                "bot": scopes_bot,
            },
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": bot_events,
            },
            "socket_mode_enabled": True,
            "org_deploy_enabled": False,
            "token_rotation_enabled": False,
        },
    }
