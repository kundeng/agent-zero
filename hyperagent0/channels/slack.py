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
from collections import OrderedDict
from typing import Any, Optional

from .base import BaseChannel, InboundMessage, OutboundMessage, register_channel
from .config import resolve_secret
from .formatter import format_for_channel

logger = logging.getLogger(__name__)


# Cap on the dedup LRU. Slack re-delivers events when our 200 ack
# doesn't reach them inside their 3s timeout; in practice we see at
# most a handful of repeats per minute, so a 1024-entry ring is
# orders of magnitude larger than needed without burning memory.
_SEEN_EVENT_ID_CAP = 1024


class SlackChannel(BaseChannel):
    """Slack adapter — Socket Mode via ``slack-bolt``.

    Spec 08 D9 hardening:

    * ``_seen_event_ids`` is a bounded LRU that drops duplicate
      ``event_id`` values Slack re-delivers when our HTTP 200 ack
      doesn't make it back in time. Without this filter every
      re-delivery would route to the agent again — burning tokens
      and posting the same reply twice.
    * ``_bot_id`` is resolved alongside ``_bot_user_id`` at
      :meth:`connect`. The ``message`` handler drops events whose
      ``bot_id`` matches our bot's id — without this filter, the
      bot's own thread replies show up as inbound messages and
      trigger an infinite-loop conversation.
    """

    channel_type = "slack"

    def __init__(self, config: dict[str, Any], *, bot_name: str = "_legacy") -> None:
        super().__init__(config, bot_name=bot_name)
        self._app: Any = None
        self._handler: Any = None
        self._client: Any = None
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        # Resolved at connect() via auth.test for spec 06 D3 mention detection.
        self._bot_user_id: Optional[str] = None
        # The platform-level bot identity. Set alongside _bot_user_id
        # at connect() so the own-message filter works.
        self._bot_id: Optional[str] = None
        # LRU set of recently-seen Slack event_ids — keys are the
        # event envelope ids, values are unused. Cap enforced by
        # _remember_event() below.
        self._seen_event_ids: "OrderedDict[str, None]" = OrderedDict()

    # ------------------------------------------------------------------
    # Hardening helpers (spec 08 D9)
    # ------------------------------------------------------------------

    def _is_duplicate_event(self, event_id: Optional[str]) -> bool:
        """Return True iff this ``event_id`` was already dispatched.

        Slack re-delivers events when our 200 ack doesn't reach
        ``slack.com`` inside their 3s window. The first delivery wins;
        every subsequent one is a no-op.
        """

        if not event_id:
            # Slack should always supply an envelope event_id, but
            # never trust input: a missing id can't be deduped, so
            # treat as non-duplicate and let the handler proceed.
            return False
        if event_id in self._seen_event_ids:
            # Refresh LRU position so we keep recently-seen ids alive.
            self._seen_event_ids.move_to_end(event_id)
            return True
        self._seen_event_ids[event_id] = None
        # Cap enforcement: drop the oldest entries on overflow.
        while len(self._seen_event_ids) > _SEEN_EVENT_ID_CAP:
            self._seen_event_ids.popitem(last=False)
        return False

    def _is_own_message(self, event: dict[str, Any]) -> bool:
        """Return True iff the event was authored by this bot.

        Slack's ``bot_id`` is the platform-level integration id (a
        single string like ``B0123…``). Comparing it against the
        ``auth.test`` response's ``bot_id`` is the canonical way to
        catch self-routed messages — the ``subtype`` filter at the
        spec-04 handler only catches Slack's *system* messages, not
        our own ``chat.postMessage`` replies.
        """

        if not self._bot_id:
            # auth.test didn't return a bot_id (e.g. user-token
            # install). Fall back to the user_id comparison so we
            # still drop self-authored messages.
            if self._bot_user_id and str(event.get("user", "")) == self._bot_user_id:
                return True
            return False
        return str(event.get("bot_id", "")) == self._bot_id

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

        # Resolve our bot identity once for spec 06 D3 mention detection
        # AND spec 08 D9 own-message filtering. ``user_id`` is the bot's
        # *user* id used in ``<@UXXXX>`` mention tokens; ``bot_id`` is
        # the platform-level integration id surfaced on every
        # ``chat.postMessage`` we send (so we can recognize and drop
        # our own messages on the inbound side).
        try:
            auth = await self._client.auth_test()
            self._bot_user_id = str(auth.get("user_id", "") or "") or None
            self._bot_id = str(auth.get("bot_id", "") or "") or None
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
                bot_name=self.bot_name,
            )

        def _event_envelope_id(event: dict, body: Optional[dict]) -> Optional[str]:
            # Slack puts the envelope id on the outer body
            # (``body["event_id"]``) — bolt-async forwards ``event`` as
            # the inner ``event`` block but the SDK also gives us the
            # body via the handler signature. We accept both shapes
            # defensively so the handler still de-dups when bolt
            # changes its wire-up.
            for src in (body or {}, event or {}):
                eid = src.get("event_id") or src.get("client_msg_id")
                if eid:
                    return str(eid)
            return None

        @self._app.event("app_mention")
        async def _on_app_mention(event, body) -> None:
            # Drop duplicate redeliveries before doing any work.
            if adapter._is_duplicate_event(_event_envelope_id(event, body)):
                return
            # Drop self-mentions (shouldn't normally happen but be
            # defensive — Slack will surface our reply if it includes
            # the bot's own user id).
            if adapter._is_own_message(event):
                return
            # Direct platform-confirmed mention — D3 happy path.
            inbound = _make_inbound(event, is_mention=True)
            await adapter._dispatch_inbound(inbound)

        @self._app.event("message")
        async def _on_message(event, body) -> None:
            # Drop duplicate redeliveries.
            if adapter._is_duplicate_event(_event_envelope_id(event, body)):
                return
            # Ignore bot/system messages (Slack's own subtype machinery).
            if event.get("subtype") is not None:
                return
            # Spec 08 D9: drop our own bot's replies before they loop.
            if adapter._is_own_message(event):
                return
            text = str(event.get("text", "") or "")
            # If our bot is mentioned in the text, the ``app_mention``
            # event handler above will fire for the same envelope and
            # dispatch the inbound — skip here to avoid two replies
            # per @-mention. Slack delivers BOTH event types for a
            # single mention; this is by design but we only want one
            # router pass.
            if bool(self._bot_user_id) and f"<@{self._bot_user_id}>" in text:
                return
            inbound = _make_inbound(event, is_mention=False)
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
