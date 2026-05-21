"""Slack channel adapter via Socket Mode (spec 04, task 2.1).

Uses ``slack-bolt`` v1.18+ (installed via the ``[slack]`` or
``[channels]`` extras). The SDK is imported **inside**
:meth:`SlackChannel.connect` so importing this module is free of the
``slack_bolt`` import cost.

Socket Mode pairs an *app-level token* (``xapp-…``) with a *bot token*
(``xoxb-…``); both come from the channel config and may use
``$$secret(KEY)`` placeholders. We thread messages by Slack
``thread_ts``: the router stores ``thread_ts`` (when present) — or the
top-level message ``ts`` for new threads — as the ``chat_id``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .base import BaseChannel, InboundMessage, OutboundMessage, register_channel
from .config import resolve_secret
from .formatter import format_for_channel

logger = logging.getLogger(__name__)


class SlackChannel(BaseChannel):
    """Slack adapter — Socket Mode via ``slack-bolt``."""

    channel_type = "slack"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._app: Any = None
        self._handler: Any = None
        self._client: Any = None
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        # Resolved at connect() via auth.test for spec 06 D3 mention detection.
        self._bot_user_id: Optional[str] = None

    async def connect(self) -> None:
        try:
            from slack_bolt.adapter.socket_mode.async_handler import (  # type: ignore
                AsyncSocketModeHandler,
            )
            from slack_bolt.async_app import AsyncApp  # type: ignore
        except Exception as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "slack-bolt is not installed. "
                "Install with `pip install hyperagent0[slack]`."
            ) from exc

        bot_token = resolve_secret(self.config.get("token", ""))
        app_token = resolve_secret(self.config.get("app_token", ""))
        if not bot_token or not app_token:
            raise RuntimeError(
                "Slack channel enabled but 'token' (bot) and/or 'app_token' "
                "(app-level) missing in channels.json."
            )

        self._app = AsyncApp(token=bot_token)
        self._client = self._app.client
        adapter = self

        # Resolve our bot user_id once for spec 06 D3 mention detection.
        try:
            auth = await self._client.auth_test()
            self._bot_user_id = str(auth.get("user_id", "") or "")
        except Exception:
            logger.warning("slack auth_test failed; is_mention via app_mention only")

        def _make_inbound(event: dict, *, is_mention: bool) -> InboundMessage:
            chat_id = event.get("thread_ts") or event.get("ts") or ""
            channel = event.get("channel") or ""
            # Slack channel types: "C..." (public), "G..." (private group),
            # "D..." (DM). Anything not D is a group/multi-user context.
            is_group = bool(channel) and not str(channel).startswith("D")
            return InboundMessage(
                channel_type="slack",
                chat_id=str(chat_id),
                user_id=str(event.get("user", "")),
                text=str(event.get("text", "") or ""),
                metadata={
                    "channel": channel,
                    "ts": event.get("ts"),
                    "thread_ts": event.get("thread_ts"),
                },
                is_mention=is_mention,
                is_group=is_group,
            )

        @self._app.event("app_mention")
        async def _on_app_mention(event, _say) -> None:
            # Direct platform-confirmed mention — D3 happy path.
            inbound = _make_inbound(event, is_mention=True)
            await adapter._dispatch_inbound(inbound)

        @self._app.event("message")
        async def _on_message(event, _say) -> None:
            # Ignore bot/system messages.
            if event.get("subtype") is not None:
                return
            # If our bot's user_id appears in the text payload as a
            # ``<@UXXXXXX>`` token, that's also a mention.
            text = str(event.get("text", "") or "")
            mention = bool(self._bot_user_id) and f"<@{self._bot_user_id}>" in text
            inbound = _make_inbound(event, is_mention=mention)
            await adapter._dispatch_inbound(inbound)

        self._handler = AsyncSocketModeHandler(self._app, app_token)
        # ``start_async`` blocks; run it as a background task on the
        # current event loop (the channels loop, set up by lifecycle.py).
        self._task = asyncio.create_task(self._handler.start_async())
        logger.info("slack adapter connected (socket mode)")

    async def disconnect(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            if self._handler is not None:
                close = getattr(self._handler, "close_async", None)
                if callable(close):
                    await close()
            if self._task is not None and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # pragma: no cover
                    pass
            logger.info("slack adapter disconnected")
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.exception("slack disconnect raised")

    async def send(self, msg: OutboundMessage) -> None:
        if self._client is None:
            logger.warning("slack send before connect; dropping message")
            return
        # The router stores ``channel`` separately from the threading
        # key; we expect it in ``msg.metadata['channel']`` (mirrored
        # from the inbound's metadata) or fall back to chat_id.
        slack_channel = msg.metadata.get("channel") if msg.metadata else None
        if slack_channel is None:
            # No channel context — assume chat_id is the channel itself
            # (DMs and bot-channel posts).
            slack_channel = msg.chat_id

        prepared = msg.metadata.get("formatted") if msg.metadata else None
        if isinstance(prepared, dict) and "blocks" in prepared:
            payload = prepared
        else:
            payload = format_for_channel(msg.text, "slack")

        try:
            await self._client.chat_postMessage(
                channel=slack_channel,
                text=payload.get("text") or msg.text,
                blocks=payload.get("blocks"),
                thread_ts=msg.reply_to or msg.chat_id,
            )
        except Exception:
            logger.exception(
                "slack chat_postMessage failed (channel=%s)", slack_channel
            )


register_channel(SlackChannel.channel_type, SlackChannel)
