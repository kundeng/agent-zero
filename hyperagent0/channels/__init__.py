"""Chat-channel adapters for hyperagent0 (spec 04).

Importing this package MUST stay cheap — no Telegram/Slack/Discord SDKs.
All heavy adapter SDKs are imported lazily inside the corresponding
adapter module's ``connect()`` (or inside a method on the adapter
class), per spec 04 task 1.3 / D-lazy-import.

The public surface of the package is:

* :class:`BaseChannel`, :class:`InboundMessage`, :class:`OutboundMessage`
  from :mod:`hyperagent0.channels.base`.
* :func:`register_channel` / :func:`get_channel_class` /
  :func:`registered_channels` from the same module.
* :func:`load_channels_config` from :mod:`hyperagent0.channels.config`.
* :class:`ChannelRouter` from :mod:`hyperagent0.channels.router`.
* :func:`format_for_channel` from :mod:`hyperagent0.channels.formatter`.
* :func:`start_enabled_channels` / :func:`stop_all_channels` from
  :mod:`hyperagent0.channels.lifecycle`.

Adapter modules (``telegram.py``, ``slack.py``, ``discord.py``) are NOT
re-exported here; importers that need them ``import
hyperagent0.channels.telegram`` (etc.) directly. That keeps a bare
``from hyperagent0 import channels`` import free of any channel SDK.
"""

from __future__ import annotations

from .base import (
    BaseChannel,
    InboundMessage,
    OutboundMessage,
    get_channel_class,
    register_channel,
    registered_channels,
)

__all__ = [
    "BaseChannel",
    "InboundMessage",
    "OutboundMessage",
    "get_channel_class",
    "register_channel",
    "registered_channels",
]
