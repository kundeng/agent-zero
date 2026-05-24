"""Base interfaces for chat-channel adapters (spec 04, restructured by spec 06).

This module is the abstract layer that every concrete channel
implementation (Telegram, Slack, Discord, ...) builds on. It is
intentionally dependency-free: importing it MUST NOT pull
``python-telegram-bot``, ``slack-bolt``, ``discord.py`` or any other
channel SDK. Concrete adapters keep their SDK imports inside
``connect()`` (or the constructor) so the cold start of
``haz status`` / ``haz stop`` stays cheap (per spec 03 D5).

Design summary (post spec 06)
-----------------------------
Spec 06 ports NanoClaw's interface shape:

* :class:`InboundEvent` (routing fields) wraps :class:`InboundMessage`
  (content fields), with an optional :class:`DeliveryAddress` for
  reply-redirection (D1).
* :class:`ChannelSetup` is the callback bundle the host hands to every
  adapter at boot (D2). Adapters call exactly one of its four methods
  per event the platform delivers.
* :class:`BaseChannel.setup` receives the bundle; :meth:`connect` then
  starts platform polling.

The legacy spec-04 ``on_message`` setter is retained as a shim so
existing tests and downstream code keep working — internally it wraps
to ``ChannelSetup.on_inbound``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Optional


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


@dataclass
class InboundMessage:
    """A message received from an external chat platform.

    Spec 06 augments the original spec-04 fields with:

    * ``is_mention`` — platform-confirmed bot-mention signal (D3). True
      when the platform's structured mention payload identifies our bot;
      adapters set this, the router prefers it over agent-name regex.
    * ``is_group`` — True when the conversation is a group/channel,
      False for DMs. Routing layer may treat the two differently
      (e.g. require mention in groups, free chat in DMs).
    * ``kind`` — ``"chat"`` for normal text, ``"chat-sdk"`` for messages
      arriving via a future Chat SDK bridge.

    The original fields stay where they are; serialization/storage in
    spec 04 used positional construction so we keep them backward
    compatible.
    """

    channel_type: str
    chat_id: str
    user_id: str
    text: str
    user_name: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # ---- spec 06 D1/D3 additions ----
    is_mention: bool = False
    is_group: bool = False
    kind: Literal["chat", "chat-sdk"] = "chat"
    # ---- spec 09 D5 addition ----
    #: Name of the bot identity that received this message. Adapters set
    #: this from their own ``self.bot_name``. Defaults to ``"_legacy"``
    #: so InboundMessages constructed by single-bot callers still route
    #: through the migration-002 ``_legacy`` row family.
    bot_name: str = "_legacy"


@dataclass
class OutboundMessage:
    """A message the agent wants to send out via a channel.

    Adapters consume ``text`` (markdown by convention) and call into
    :mod:`hyperagent0.channels.formatter` to translate to whatever
    markup the platform expects.

    Spec 09 D5: ``bot_name`` lets the router pick the right adapter
    when multiple bots are registered for the same ``channel_type``.
    Defaults to ``"_legacy"`` so single-bot installs work unchanged.
    """

    chat_id: str
    text: str
    reply_to: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    bot_name: str = "_legacy"


@dataclass
class DeliveryAddress:
    """Where to deliver a reply (spec 06 D1).

    ``(channel_type, platform_id, thread_id)`` is a complete routing
    triple. When attached to an :class:`InboundEvent` as ``reply_to``,
    it overrides the default behavior of "reply on the inbound's own
    channel" — used by admin-transport adapters (CLI) that want the
    agent's reply echoed somewhere other than where the prompt came in.

    Agents themselves cannot set ``reply_to`` — it is a router-layer
    concept set only by external adapters carrying operator intent.
    """

    channel_type: str
    platform_id: str
    thread_id: Optional[str] = None


@dataclass
class InboundEvent:
    """Routing-and-content envelope handed to the router (spec 06 D1).

    ``channel_type`` + ``platform_id`` + ``thread_id`` identify which
    messaging group / session this event belongs to. ``message`` is the
    content. ``reply_to``, when set, redirects the agent's reply to a
    different address than the inbound's origin.
    """

    channel_type: str
    platform_id: str
    thread_id: Optional[str]
    message: InboundMessage
    reply_to: Optional[DeliveryAddress] = None


# ---------------------------------------------------------------------------
# Host-supplied callback bundle (the "ChannelSetup" interface, D2)
# ---------------------------------------------------------------------------


class ChannelSetup(abc.ABC):
    """Callback bundle the host hands to every adapter at boot.

    Mirrors NanoClaw's ``ChannelSetup`` interface
    (``src/channels/adapter.ts``). Adapters invoke exactly one of these
    methods per inbound event the platform delivers; the host's router
    implementation decides what to do with it.

    Concrete implementations live in :mod:`hyperagent0.channels.router`.
    """

    @abc.abstractmethod
    async def on_inbound(
        self,
        platform_id: str,
        thread_id: Optional[str],
        message: InboundMessage,
    ) -> None:
        """Normal chat message path — most adapters use only this."""

    @abc.abstractmethod
    async def on_inbound_event(self, event: InboundEvent) -> None:
        """Admin-transport path: caller may set ``reply_to`` to redirect.

        Adapters that carry operator intent (e.g. a CLI tool routing a
        message to one channel but wanting the reply echoed to the
        terminal) use this instead of :meth:`on_inbound`.
        """

    @abc.abstractmethod
    async def on_metadata(
        self,
        platform_id: str,
        *,
        name: Optional[str] = None,
        is_group: Optional[bool] = None,
    ) -> None:
        """Adapter discovered conversation metadata (chat name, group flag)."""

    @abc.abstractmethod
    async def on_action(
        self,
        question_id: str,
        selected_option: str,
        user_id: str,
    ) -> None:
        """User clicked an interactive button/card from an ask-question card."""


# ---------------------------------------------------------------------------
# Channel ABC
# ---------------------------------------------------------------------------


OnMessageCallback = Callable[[InboundMessage], Awaitable[None]]


class BaseChannel(abc.ABC):
    """Abstract base for chat-channel adapters.

    Concrete subclasses must:
      * set :pyattr:`channel_type` to their registry key,
      * implement :meth:`connect`, :meth:`disconnect`, :meth:`send`,
      * call into :pyattr:`channel_setup` for every inbound event the
        platform delivers.

    The legacy ``on_message`` setter (spec 04) is preserved as a
    deprecation shim — when set, it is wrapped into a minimal
    :class:`ChannelSetup` whose only active method is ``on_inbound``.
    """

    #: Subclasses override this with their registry key.
    channel_type: str = ""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        bot_name: str = "_legacy",
    ) -> None:
        # ``config`` is the per-channel dict produced by
        # :mod:`hyperagent0.channels.config`. Secrets inside it may
        # still be in their ``$$secret(KEY)`` placeholder form; adapters
        # resolve them lazily inside :meth:`connect`.
        self.config = config
        #: Spec 09 D5: each adapter instance belongs to exactly one bot.
        #: ``bot_name`` defaults to ``"_legacy"`` for callers that predate
        #: the multi-bot lifecycle wiring — matches the migration-002
        #: column default so single-bot installs keep working unchanged.
        self.bot_name = bot_name
        self._channel_setup: Optional[ChannelSetup] = None
        # Legacy spec-04 callback (still supported via the on_message shim).
        self._on_message: Optional[OnMessageCallback] = None

    # ------------------------------------------------------------------
    # Spec 06 wire-up — preferred
    # ------------------------------------------------------------------

    def setup(self, channel_setup: ChannelSetup) -> None:
        """Hand the adapter its host callback bundle (spec 06 D2).

        Called synchronously by :mod:`hyperagent0.channels.lifecycle`
        before :meth:`connect`. Adapters store the reference and invoke
        one of its four methods per inbound event.
        """

        self._channel_setup = channel_setup

    @property
    def channel_setup(self) -> Optional[ChannelSetup]:
        return self._channel_setup

    # ------------------------------------------------------------------
    # Spec 04 wire-up — kept as deprecation shim
    # ------------------------------------------------------------------

    @property
    def on_message(self) -> Optional[OnMessageCallback]:
        return self._on_message

    @on_message.setter
    def on_message(self, cb: Optional[OnMessageCallback]) -> None:
        """Legacy single-callback setter (spec 04).

        New code should use :meth:`setup` with a full
        :class:`ChannelSetup`. Existing tests and downstream code that
        set ``adapter.on_message = router.handle_inbound`` keep working
        because :meth:`_dispatch_inbound` falls back to it when no
        :class:`ChannelSetup` is attached.
        """

        self._on_message = cb

    # ------------------------------------------------------------------
    # Dispatch helpers
    # ------------------------------------------------------------------

    async def _dispatch_inbound(self, msg: InboundMessage) -> None:
        """Adapter-facing helper for the simple inbound path.

        Prefers the spec-06 :class:`ChannelSetup.on_inbound`. Falls back
        to the legacy ``on_message`` callback if no setup was attached.
        Swallows exceptions so a buggy host never takes the adapter's
        long-poll loop down.
        """

        if self._channel_setup is not None:
            try:
                await self._channel_setup.on_inbound(
                    msg.chat_id,
                    msg.metadata.get("thread_id"),
                    msg,
                )
            except Exception:  # pragma: no cover - defensive
                self._log_dispatch_failure("on_inbound")
            return

        if self._on_message is None:
            return
        try:
            await self._on_message(msg)
        except Exception:  # pragma: no cover - defensive
            self._log_dispatch_failure("on_message")

    async def _dispatch_event(self, event: InboundEvent) -> None:
        """Adapter-facing helper for the routing-aware (reply_to) path.

        Used by admin-transport adapters that need to set ``reply_to``.
        Standard chat adapters should prefer :meth:`_dispatch_inbound`.
        """

        if self._channel_setup is None:
            # Fall back to inbound-only dispatch; the reply_to override
            # has no effect when going through the legacy callback.
            await self._dispatch_inbound(event.message)
            return
        try:
            await self._channel_setup.on_inbound_event(event)
        except Exception:  # pragma: no cover - defensive
            self._log_dispatch_failure("on_inbound_event")

    async def _dispatch_metadata(
        self,
        platform_id: str,
        *,
        name: Optional[str] = None,
        is_group: Optional[bool] = None,
    ) -> None:
        if self._channel_setup is None:
            return
        try:
            await self._channel_setup.on_metadata(
                platform_id, name=name, is_group=is_group
            )
        except Exception:  # pragma: no cover - defensive
            self._log_dispatch_failure("on_metadata")

    async def _dispatch_action(
        self,
        question_id: str,
        selected_option: str,
        user_id: str,
    ) -> None:
        if self._channel_setup is None:
            return
        try:
            await self._channel_setup.on_action(
                question_id, selected_option, user_id
            )
        except Exception:  # pragma: no cover - defensive
            self._log_dispatch_failure("on_action")

    def _log_dispatch_failure(self, hook: str) -> None:
        import logging

        logging.getLogger(__name__).exception(
            "ChannelSetup.%s raised for channel %s", hook, self.channel_type
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
