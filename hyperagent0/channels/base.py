"""Base interfaces for chat-channel adapters (spec 04, task 1.1).

This module is the abstract layer that every concrete channel
implementation (Telegram, Slack, Discord, ...) builds on. It is
intentionally dependency-free: importing it MUST NOT pull
``python-telegram-bot``, ``slack-bolt``, ``discord.py`` or any other
channel SDK. Concrete adapters keep their SDK imports inside
``connect()`` (or the constructor) so the cold start of
``haz status`` / ``haz stop`` stays cheap (per spec 03 D5).

Design summary
--------------
* :class:`BaseChannel` is a small async ABC with four lifecycle hooks:
  ``connect``, ``disconnect``, ``send``, and an ``on_message`` callback
  set by the router.
* :class:`InboundMessage` and :class:`OutboundMessage` are the wire
  shapes that flow through the router. They are channel-agnostic; the
  ``channel_type`` field is the discriminator the router uses to
  dispatch replies back through the right adapter.
* A tiny registry (:func:`register_channel` / :func:`get_channel_class`)
  lets the daemon enumerate adapters by name without importing every
  SDK at startup.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


@dataclass
class InboundMessage:
    """A message received from an external chat platform.

    Attributes
    ----------
    channel_type
        Short identifier of the originating channel (``"telegram"``,
        ``"slack"``, ``"discord"``, ...). Matches the registry key used
        by :func:`register_channel`.
    chat_id
        Stable platform identifier for the conversation thread. The
        router uses ``(channel_type, chat_id)`` as the key into the
        SQLite mapping; it MUST survive restarts.
    user_id
        Platform-specific sender identifier. Used for the allow-list
        check; empty string if the platform does not provide one.
    text
        Plain text payload. Adapters should strip platform-specific
        formatting before constructing the message.
    user_name
        Display name of the sender, when available. Purely advisory —
        used for log lines, not for authorization.
    attachments
        Optional list of attachment descriptors (P3 territory; kept here
        so the dataclass is forward-compatible).
    metadata
        Free-form bag for platform-specific extras (e.g. Telegram
        ``message_id``, Slack ``thread_ts``) that an adapter wants to
        echo back when replying.
    received_at
        Timestamp the inbound was constructed at. UTC.
    """

    channel_type: str
    chat_id: str
    user_id: str
    text: str
    user_name: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class OutboundMessage:
    """A message the agent wants to send out via a channel.

    Adapters consume ``text`` (markdown by convention) and call into
    :mod:`hyperagent0.channels.formatter` to translate to whatever
    markup the platform expects.
    """

    chat_id: str
    text: str
    reply_to: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Channel ABC
# ---------------------------------------------------------------------------


OnMessageCallback = Callable[[InboundMessage], Awaitable[None]]


class BaseChannel(abc.ABC):
    """Abstract base for chat-channel adapters.

    Concrete subclasses must:
      * set :pyattr:`channel_type` to their registry key,
      * implement :meth:`connect`, :meth:`disconnect`, :meth:`send`,
      * call :pyattr:`on_message` with an :class:`InboundMessage` for
        every inbound from the platform.
    """

    #: Subclasses override this with their registry key.
    channel_type: str = ""

    def __init__(self, config: dict[str, Any]) -> None:
        # ``config`` is the per-channel dict produced by
        # :mod:`hyperagent0.channels.config`. Secrets inside it may
        # still be in their ``$$secret(KEY)`` placeholder form; adapters
        # resolve them lazily inside :meth:`connect`.
        self.config = config
        self._on_message: Optional[OnMessageCallback] = None

    # ------------------------------------------------------------------
    # Wire-up
    # ------------------------------------------------------------------

    @property
    def on_message(self) -> Optional[OnMessageCallback]:
        return self._on_message

    @on_message.setter
    def on_message(self, cb: Optional[OnMessageCallback]) -> None:
        self._on_message = cb

    async def _dispatch_inbound(self, msg: InboundMessage) -> None:
        """Helper subclasses call when they receive an inbound message.

        Swallows exceptions so a buggy router never takes the adapter's
        long-poll loop down.
        """

        if self._on_message is None:
            return
        try:
            await self._on_message(msg)
        except Exception:  # pragma: no cover - defensive
            # Adapter loops are long-running; we log rather than die.
            import logging

            logging.getLogger(__name__).exception(
                "on_message callback raised for channel %s", self.channel_type
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the channel — bring up the SDK client, start polling."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Tear the channel down. Called from graceful shutdown."""

    @abc.abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Push an outbound message to the platform."""


# ---------------------------------------------------------------------------
# Tiny channel registry
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, type[BaseChannel]] = {}


def register_channel(name: str, cls: type[BaseChannel]) -> None:
    """Register a channel adapter class under ``name``.

    Adapter modules call this at import time; the daemon imports the
    adapter module only when the corresponding channel is enabled, so
    the registry stays small and SDK imports stay lazy.
    """

    if not name:
        raise ValueError("channel name must be non-empty")
    _REGISTRY[name] = cls


def get_channel_class(name: str) -> Optional[type[BaseChannel]]:
    return _REGISTRY.get(name)


def registered_channels() -> list[str]:
    return sorted(_REGISTRY.keys())
