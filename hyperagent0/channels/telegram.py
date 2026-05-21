"""Telegram channel adapter (spec 04, task 1.3).

Uses ``python-telegram-bot`` v21+ (installed via the ``[telegram]`` or
``[channels]`` extras declared in ``pyproject.toml``). The SDK is
imported **inside** :meth:`TelegramChannel.connect` so a bare
``import hyperagent0.channels`` (or even
``import hyperagent0.channels.telegram``) does not pay the python-telegram-bot
import cost; only starting the adapter does.

Inbound flow
------------
1. ``Application.builder().token(...).build()`` constructs the SDK app.
2. A single ``MessageHandler(filters.TEXT & ~filters.COMMAND, _handle)``
   is wired in; ``_handle`` converts the Telegram ``Update`` to an
   :class:`InboundMessage` and dispatches via the router-bound
   ``on_message`` callback.
3. ``application.start()`` + ``application.updater.start_polling()`` is
   used instead of ``run_polling()`` so we keep control of the event
   loop (the daemon owns it).

Outbound flow
-------------
``send()`` formats the markdown via
:func:`hyperagent0.channels.formatter.format_for_channel` (returns a
list of HTML chunks) and posts each chunk with
``parse_mode=HTML``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import BaseChannel, InboundMessage, OutboundMessage, register_channel
from .config import resolve_secret
from .formatter import format_for_channel

logger = logging.getLogger(__name__)


class TelegramChannel(BaseChannel):
    """Telegram adapter — long-polling via ``python-telegram-bot``."""

    channel_type = "telegram"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        # All SDK objects stay None until ``connect()`` resolves them.
        self._application: Any = None
        self._bot: Any = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        # Lazy import. Failures here surface to the lifecycle helper,
        # which logs and continues with other channels.
        try:
            from telegram import Update  # type: ignore
            from telegram.ext import (  # type: ignore
                Application,
                ContextTypes,
                MessageHandler,
                filters,
            )
        except Exception as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "python-telegram-bot is not installed. "
                "Install with `pip install hyperagent0[telegram]`."
            ) from exc

        token = resolve_secret(self.config.get("token", ""))
        if not token:
            raise RuntimeError(
                "Telegram channel enabled but no token configured "
                "(set 'token' in channels.json)."
            )

        self._application = (
            Application.builder().token(token).build()
        )
        self._bot = self._application.bot

        adapter = self  # closure capture for the handler below

        # Resolve bot identity AFTER initialize() so getMe() has been called.
        # We store both username (for entity-type='mention') and id (for
        # entity-type='text_mention') so spec 06 D3 mention detection works.
        await self._application.initialize()
        bot = self._application.bot
        bot_username = (getattr(bot, "username", "") or "").lstrip("@")
        bot_id = getattr(bot, "id", None)
        self._bot_username = bot_username
        self._bot_id = bot_id

        def _detect_is_mention(message: Any) -> bool:
            """Spec 06 D3: platform-confirmed bot-mention detection.

            Telegram's ``message.entities`` carries structured mention
            payloads. Two relevant types:
              * ``mention``       — text mention like ``@your_bot_username``
              * ``text_mention``  — privacy-mode mention with a ``user``
                payload pointing at the bot account
            We check both. Adapter-name regex (the spec-04 fallback)
            never sees a hit on Telegram bots whose username doesn't
            match the agent's display name.
            """

            text = getattr(message, "text", "") or ""
            entities = getattr(message, "entities", None) or []
            for ent in entities:
                etype = getattr(ent, "type", None)
                if etype == "text_mention":
                    user = getattr(ent, "user", None)
                    if user is not None and getattr(user, "id", None) == bot_id:
                        return True
                elif etype == "mention" and bot_username:
                    start = getattr(ent, "offset", 0)
                    length = getattr(ent, "length", 0)
                    snippet = text[start : start + length].lstrip("@").lower()
                    if snippet == bot_username.lower():
                        return True
            return False

        async def _handle(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
            message = getattr(update, "effective_message", None)
            if message is None or not getattr(message, "text", None):
                return
            chat = getattr(update, "effective_chat", None)
            user = getattr(update, "effective_user", None)
            chat_type = getattr(chat, "type", None) if chat else None
            is_group = chat_type in ("group", "supergroup", "channel")
            inbound = InboundMessage(
                channel_type="telegram",
                chat_id=str(chat.id) if chat is not None else "",
                user_id=str(user.id) if user is not None else "",
                user_name=(
                    getattr(user, "username", "") or getattr(user, "full_name", "")
                ) if user is not None else "",
                text=message.text or "",
                metadata={
                    "message_id": getattr(message, "message_id", None),
                    "chat_type": chat_type,
                },
                is_mention=_detect_is_mention(message),
                is_group=is_group,
            )
            await adapter._dispatch_inbound(inbound)

        self._application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, _handle)
        )

        # initialize() was already called above (we needed bot identity for
        # is_mention detection). Just start the application here.
        await self._application.start()
        # ``updater`` is the polling task; starting it here makes the
        # adapter live.
        updater = getattr(self._application, "updater", None)
        if updater is not None:
            await updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("telegram adapter connected")

    async def disconnect(self) -> None:
        if self._application is None or self._stopped:
            return
        self._stopped = True
        try:
            updater = getattr(self._application, "updater", None)
            if updater is not None and getattr(updater, "running", False):
                await updater.stop()
            await self._application.stop()
            await self._application.shutdown()
            logger.info("telegram adapter disconnected")
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.exception("telegram disconnect raised")

    async def send(self, msg: OutboundMessage) -> None:
        if self._bot is None:
            logger.warning("telegram send before connect; dropping message")
            return
        # If the router already prepared the formatted payload, use it.
        prepared = msg.metadata.get("formatted") if msg.metadata else None
        if isinstance(prepared, list) and prepared:
            chunks = prepared
        else:
            chunks = format_for_channel(msg.text, "telegram")

        reply_to: Optional[int] = None
        if msg.reply_to is not None:
            try:
                reply_to = int(msg.reply_to)
            except (TypeError, ValueError):
                reply_to = None

        for chunk in chunks:
            if not chunk:
                continue
            try:
                await self._bot.send_message(
                    chat_id=msg.chat_id,
                    text=chunk,
                    parse_mode="HTML",
                    reply_to_message_id=reply_to,
                )
            except Exception:
                logger.exception(
                    "telegram send_message failed (chat_id=%s)", msg.chat_id
                )
                # Only attempt to thread the first chunk.
            reply_to = None


# Register at import time. Registration is cheap (dict insert) and does
# NOT pull the SDK — the SDK import is inside connect().
register_channel(TelegramChannel.channel_type, TelegramChannel)
