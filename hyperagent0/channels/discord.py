"""Discord channel adapter (spec 04, task 2.2).

Uses ``discord.py`` v2.3+ (installed via the ``[discord]`` or
``[channels]`` extras). The SDK is imported inside
:meth:`DiscordChannel.connect` so importing this module is free of
``discord``'s import cost.

Threading model: Discord channels are persistent and don't have an
implicit thread concept, so the adapter uses the channel ID as
``chat_id``. Guild/channel allowlists (separate from the global
``allowed_chats``) are honored when provided in the channel config as
``allowed_guilds`` / ``allowed_channels``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .base import BaseChannel, InboundMessage, OutboundMessage, register_channel
from .config import resolve_secret
from .formatter import format_for_channel

logger = logging.getLogger(__name__)


class DiscordChannel(BaseChannel):
    """Discord adapter — gateway client via ``discord.py``."""

    channel_type = "discord"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._client: Any = None
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        # Adapter-local allowlists (the global ``allowed_users`` /
        # ``allowed_chats`` lists still apply via the router).
        self._allowed_guilds: set[str] = {
            str(g) for g in (config.get("allowed_guilds") or [])
        }
        self._allowed_channels: set[str] = {
            str(c) for c in (config.get("allowed_channels") or [])
        }

    async def connect(self) -> None:
        try:
            import discord  # type: ignore
        except Exception as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "discord.py is not installed. "
                "Install with `pip install hyperagent0[discord]`."
            ) from exc

        token = resolve_secret(self.config.get("token", ""))
        if not token:
            raise RuntimeError(
                "Discord channel enabled but no token configured "
                "(set 'token' in channels.json)."
            )

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        adapter = self

        @self._client.event
        async def on_ready() -> None:  # pragma: no cover - network event
            logger.info("discord adapter ready as %s", self._client.user)

        @self._client.event
        async def on_message(message: "discord.Message") -> None:
            # Ignore self and other bots.
            if message.author.bot:
                return
            guild_id = str(message.guild.id) if message.guild else ""
            channel_id = str(message.channel.id)
            if adapter._allowed_guilds and guild_id not in adapter._allowed_guilds:
                return
            if (
                adapter._allowed_channels
                and channel_id not in adapter._allowed_channels
            ):
                return
            inbound = InboundMessage(
                channel_type="discord",
                chat_id=channel_id,
                user_id=str(message.author.id),
                user_name=str(getattr(message.author, "name", "") or ""),
                text=message.content or "",
                metadata={
                    "message_id": str(message.id),
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                },
            )
            await adapter._dispatch_inbound(inbound)

        # Run the gateway loop as a background task.
        self._task = asyncio.create_task(self._client.start(token))
        logger.info("discord adapter connecting")

    async def disconnect(self) -> None:
        if self._stopped or self._client is None:
            return
        self._stopped = True
        try:
            await self._client.close()
            if self._task is not None and not self._task.done():
                try:
                    await self._task
                except Exception:  # pragma: no cover - best-effort
                    pass
            logger.info("discord adapter disconnected")
        except Exception:  # pragma: no cover - best-effort
            logger.exception("discord disconnect raised")

    async def send(self, msg: OutboundMessage) -> None:
        if self._client is None:
            logger.warning("discord send before connect; dropping message")
            return
        try:
            chan_id = int(msg.chat_id)
        except (TypeError, ValueError):
            logger.error("discord chat_id is not an int: %r", msg.chat_id)
            return
        channel = self._client.get_channel(chan_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(chan_id)
            except Exception:
                logger.exception("discord fetch_channel failed: %s", chan_id)
                return

        prepared = msg.metadata.get("formatted") if msg.metadata else None
        chunks = prepared if isinstance(prepared, list) else format_for_channel(
            msg.text, "discord"
        )
        for chunk in chunks:
            if not chunk:
                continue
            try:
                await channel.send(chunk)
            except Exception:
                logger.exception("discord channel.send failed for %s", chan_id)


register_channel(DiscordChannel.channel_type, DiscordChannel)
