"""Channel → AgentContext router (spec 04, restructured by spec 06).

Inbound messages from any channel arrive via the
:class:`hyperagent0.channels.base.ChannelSetup` contract that the
router implements. The router:

1. Validates the sender against the channel's allow-list.
2. Honors ``require_mention`` (spec 06 D3) for groups.
3. Looks up the persistent ``(channel_type, chat_id) → context_id``
   mapping in SQLite (``~/.hyperagent0/channels.db``), upgrading the
   schema via the migrator (spec 06 D6) on first open.
4. Resumes the existing :class:`agent.AgentContext` if one is live, or
   creates a fresh one (activating the channel-bound project).
5. Dispatches the message via ``context.communicate(...)``.
6. After the agent finishes, ships the reply back via the originating
   channel adapter — or via ``event.reply_to`` if the inbound carried
   an override (spec 06 D1).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from .base import (
    BaseChannel,
    ChannelSetup,
    DeliveryAddress,
    InboundEvent,
    InboundMessage,
    OutboundMessage,
)
from .config import ChannelConfig
from .formatter import format_for_channel
from .migrations.migrator import Migrator

if TYPE_CHECKING:  # pragma: no cover - import-cycle guard
    from agent import AgentContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQLite-backed mapping store
# ---------------------------------------------------------------------------


def default_db_path() -> Path:
    return Path(os.path.expanduser("~/.hyperagent0/channels.db"))


#: Default bot_name used when callers haven't migrated to multi-bot
#: keying yet. Matches the DEFAULT clause in migration 002 so existing
#: rows and new rows from single-bot callers share the same key.
LEGACY_BOT_NAME = "_legacy"


class ThreadStore:
    """SQLite wrapper for the (channel_type, bot_name, chat_id) → context mapping.

    Spec 06: schema management moved into :class:`Migrator` (numbered
    .sql files in ``migrations/``). Construction applies any pending
    migrations idempotently.

    Spec 09 (D3 / migration 002): composite key extended with
    ``bot_name`` so two bots on the same platform can DM the same chat
    id without collision. ``bot_name`` defaults to
    :data:`LEGACY_BOT_NAME` for callers that predate the multi-bot
    wiring — the default value matches the column default in the
    migration so legacy rows and new single-bot-shape inserts coexist.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.path = db_path or default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        Migrator(self._conn, lock=self._lock).upgrade()

    def get(
        self,
        channel_type: str,
        chat_id: str,
        *,
        bot_name: str = LEGACY_BOT_NAME,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT channel_type, bot_name, chat_id, context_id, project_name, last_active "
                "FROM thread_map "
                "WHERE channel_type = ? AND bot_name = ? AND chat_id = ?",
                (channel_type, bot_name, chat_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "channel_type": row[0],
            "bot_name": row[1],
            "chat_id": row[2],
            "context_id": row[3],
            "project_name": row[4],
            "last_active": row[5],
        }

    def upsert(
        self,
        channel_type: str,
        chat_id: str,
        context_id: str,
        project_name: Optional[str] = None,
        *,
        bot_name: str = LEGACY_BOT_NAME,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO thread_map(channel_type, bot_name, chat_id, context_id, project_name, last_active)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_type, bot_name, chat_id) DO UPDATE SET
                    context_id   = excluded.context_id,
                    project_name = excluded.project_name,
                    last_active  = excluded.last_active
                """,
                (channel_type, bot_name, chat_id, context_id, project_name, now),
            )
            self._conn.commit()

    def touch(
        self,
        channel_type: str,
        chat_id: str,
        *,
        bot_name: str = LEGACY_BOT_NAME,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE thread_map SET last_active = ? "
                "WHERE channel_type = ? AND bot_name = ? AND chat_id = ?",
                (now, channel_type, bot_name, chat_id),
            )
            self._conn.commit()

    def all_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT channel_type, bot_name, chat_id, context_id, project_name, last_active "
                "FROM thread_map ORDER BY last_active DESC"
            )
            rows = cur.fetchall()
        return [
            {
                "channel_type": r[0],
                "bot_name": r[1],
                "chat_id": r[2],
                "context_id": r[3],
                "project_name": r[4],
                "last_active": r[5],
            }
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


#: Optional override hook used by tests to skip the real agent core.
#: When set, the router calls this instead of ``context.communicate``
#: and skips the AgentContext lookup. Signature:
#: ``(inbound, channel, context_id_hint) -> awaitable[str | None]``
ReplyFactory = Callable[[InboundMessage, BaseChannel, Optional[str]], Any]


class ChannelRouter(ChannelSetup):
    """Routes inbound channel messages to AgentContexts and back.

    The router is created once by the daemon. Each adapter is handed a
    reference to this router via :meth:`BaseChannel.setup` before
    :meth:`BaseChannel.connect` returns.

    Spec 06: :class:`ChannelRouter` implements :class:`ChannelSetup`
    directly, so adapters call ``self._channel_setup.on_inbound(...)``
    etc. The legacy ``register(channel)`` method still wires up the
    spec-04 ``on_message`` callback for older adapter code.
    """

    def __init__(
        self,
        channels: Optional[Dict[Any, BaseChannel]] = None,
        channel_configs: Optional[Dict[Any, ChannelConfig]] = None,
        store: Optional[ThreadStore] = None,
        reply_factory: Optional[ReplyFactory] = None,
    ) -> None:
        # Spec 09 D5: channel adapters and per-channel configs are
        # indexed by ``(channel_type, bot_name)``. Legacy callers
        # passing a plain ``str`` key are wrapped as
        # ``(key, "_legacy")``.
        self.channels: Dict[tuple[str, str], BaseChannel] = {
            _normalize_channel_key(k): v for k, v in (channels or {}).items()
        }
        self.channel_configs: Dict[tuple[str, str], ChannelConfig] = {
            _normalize_channel_key(k): v for k, v in (channel_configs or {}).items()
        }
        self.store = store or ThreadStore()
        # ``reply_factory`` is the test seam — see module docstring.
        self.reply_factory = reply_factory

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self, channel: BaseChannel, config: Optional[ChannelConfig] = None
    ) -> None:
        """Attach a channel adapter.

        Wires both the spec-06 :class:`ChannelSetup` path (preferred)
        and the spec-04 ``on_message`` callback (legacy) so adapters
        that haven't migrated still get inbound dispatch.

        Spec 09 D5: the adapter is registered under the
        ``(channel_type, bot_name)`` tuple so multiple bots on the
        same platform get separate slots.
        """

        key = (channel.channel_type, channel.bot_name)
        self.channels[key] = channel
        if config is not None:
            self.channel_configs[key] = config
        channel.setup(self)
        channel.on_message = self.handle_inbound  # legacy path

    # ------------------------------------------------------------------
    # ChannelSetup implementation (spec 06 D2)
    # ------------------------------------------------------------------

    async def on_inbound(
        self,
        platform_id: str,
        thread_id: Optional[str],
        message: InboundMessage,
    ) -> None:
        await self.handle_inbound(message)

    async def on_inbound_event(self, event: InboundEvent) -> None:
        """Routing-aware path — honors ``event.reply_to`` override."""

        await self._handle_inbound_impl(event.message, reply_to=event.reply_to)

    async def on_metadata(
        self,
        platform_id: str,
        *,
        name: Optional[str] = None,
        is_group: Optional[bool] = None,
    ) -> None:
        # Best-effort: refresh last_active so the chat doesn't look stale.
        # Channel-name persistence can be added in a future migration.
        # We don't know the channel_type/bot_name here without context;
        # the touch is a no-op when no row exists, so we fan out over
        # every (channel_type, bot_name) pair we know.
        if not platform_id:
            return
        for ct, bot_name in list(self.channels.keys()):
            self.store.touch(ct, platform_id, bot_name=bot_name)

    async def on_action(
        self,
        question_id: str,
        selected_option: str,
        user_id: str,
    ) -> None:
        """Inject a button click as a user message into the agent.

        Spec 06 D7 / open question: no upstream patch needed — we route
        the click through the standard :meth:`AgentContext.communicate`
        public API as a synthetic user message. ``question_id`` is the
        opaque token the original ask-question card embedded; we don't
        need to interpret it here, the agent's monologue does.
        """

        # Find an existing AgentContext that has issued this question_id.
        # For v1 we don't track question_id → context mapping; the
        # router scans live contexts. If none match, log + drop.
        ctx = self._find_context_with_question(question_id)
        if ctx is None:
            logger.info(
                "on_action: no live AgentContext owns question %s; dropping",
                question_id,
            )
            return
        try:
            from agent import UserMessage  # type: ignore
        except Exception as exc:
            logger.error("agent.UserMessage not importable: %s", exc)
            return
        synthetic_text = (
            f"[action] user {user_id} selected: {selected_option}"
        )
        try:
            ctx.communicate(UserMessage(message=synthetic_text))
        except Exception:
            logger.exception("on_action communicate() failed")

    def _find_context_with_question(self, question_id: str) -> Optional["AgentContext"]:
        """Best-effort scan of live AgentContexts looking for ``question_id``.

        Upstream doesn't expose a question-id → context index, so we
        fall back to inspecting each live context's log for an entry
        marked with this id. This is O(contexts × log) and good enough
        for v1 of the on_action plumbing.
        """

        try:
            from agent import AgentContext  # type: ignore
        except Exception:
            return None
        contexts = getattr(AgentContext, "all", None)
        if callable(contexts):
            try:
                live = list(contexts())
            except Exception:
                live = []
        else:
            live = []
        for ctx in live:
            log = getattr(ctx, "log", None)
            items = getattr(log, "logs", None) or []
            for item in items:
                kvps = getattr(item, "kvps", None) or {}
                if kvps.get("question_id") == question_id:
                    return ctx
        return None

    # ------------------------------------------------------------------
    # Inbound handling (used by both new and legacy paths)
    # ------------------------------------------------------------------

    async def handle_inbound(self, msg: InboundMessage) -> None:
        """Process a single inbound message end-to-end (legacy entry point)."""

        await self._handle_inbound_impl(msg, reply_to=None)

    async def _handle_inbound_impl(
        self,
        msg: InboundMessage,
        *,
        reply_to: Optional[DeliveryAddress],
    ) -> None:
        key = (msg.channel_type, msg.bot_name)
        cfg = self.channel_configs.get(key)
        if cfg is not None:
            if not cfg.is_user_allowed(msg.user_id):
                logger.info(
                    "channel %s/%s rejected user %s (not in allow-list)",
                    msg.channel_type,
                    msg.bot_name,
                    msg.user_id,
                )
                return
            if not cfg.is_chat_allowed(msg.chat_id):
                logger.info(
                    "channel %s/%s rejected chat %s (not in allow-list)",
                    msg.channel_type,
                    msg.bot_name,
                    msg.chat_id,
                )
                return
            # Spec 06 D3: require_mention enforces platform-confirmed
            # mention in groups. DMs (is_group=False) always pass.
            if (
                cfg.require_mention
                and msg.is_group
                and not msg.is_mention
            ):
                logger.debug(
                    "channel %s/%s chat %s: group msg without mention; ignored",
                    msg.channel_type,
                    msg.bot_name,
                    msg.chat_id,
                )
                return

        existing = self.store.get(msg.channel_type, msg.chat_id, bot_name=msg.bot_name)
        context_id_hint = existing["context_id"] if existing else None

        # If a test reply factory is set, take that path and skip the
        # AgentContext machinery entirely.
        if self.reply_factory is not None:
            adapter = self.channels.get(key)
            if adapter is None:
                logger.error(
                    "no adapter registered for %s/%s",
                    msg.channel_type,
                    msg.bot_name,
                )
                return
            reply = await _maybe_await(
                self.reply_factory(msg, adapter, context_id_hint)
            )
            if reply:
                await self._send_reply(msg, str(reply), reply_to=reply_to)
            self.store.upsert(
                msg.channel_type,
                msg.chat_id,
                context_id_hint or "test",
                None,
                bot_name=msg.bot_name,
            )
            return

        context, project_name = self._get_or_create_context(msg, cfg, existing)
        if context is None:
            logger.error(
                "could not obtain AgentContext for %s/%s/%s",
                msg.channel_type,
                msg.bot_name,
                msg.chat_id,
            )
            return

        # Persist the (possibly new) mapping before dispatch so a crash
        # mid-monologue still leaves the chat resumable.
        self.store.upsert(
            msg.channel_type,
            msg.chat_id,
            context.id,
            project_name,
            bot_name=msg.bot_name,
        )

        reply_text = await self._dispatch_to_context(context, msg)
        if reply_text:
            await self._send_reply(msg, reply_text, reply_to=reply_to)

    # ------------------------------------------------------------------
    # Context lifecycle
    # ------------------------------------------------------------------

    def _get_or_create_context(
        self,
        msg: InboundMessage,
        cfg: Optional[ChannelConfig],
        existing: Optional[Dict[str, Any]],
    ) -> tuple[Optional["AgentContext"], Optional[str]]:
        """Return (context, project_name). Imports agent lazily."""

        try:
            from agent import AgentContext  # type: ignore
        except Exception as exc:
            logger.error("agent module not importable: %s", exc)
            return None, None

        if existing:
            ctx = AgentContext.get(existing["context_id"])
            if ctx is not None:
                return ctx, existing.get("project_name")

        try:
            import initialize  # type: ignore

            config = initialize.initialize_agent()
        except Exception as exc:
            logger.error("failed to build AgentConfig for new channel context: %s", exc)
            return None, None

        ctx = AgentContext(
            config=config,
            name=f"channel:{msg.channel_type}:{msg.chat_id}",
        )

        project_name = cfg.project_for_chat(msg.chat_id) if cfg else None
        if project_name:
            try:
                from python.helpers import projects as projects_helper  # type: ignore

                projects_helper.activate_project(ctx.id, project_name)
            except Exception as exc:
                logger.warning(
                    "could not activate project %s for new channel context: %s",
                    project_name,
                    exc,
                )
                project_name = None

        return ctx, project_name

    async def _dispatch_to_context(
        self, context: "AgentContext", msg: InboundMessage
    ) -> Optional[str]:
        try:
            from agent import UserMessage  # type: ignore
        except Exception as exc:
            logger.error("agent.UserMessage not importable: %s", exc)
            return None

        try:
            task = context.communicate(UserMessage(message=msg.text))
        except Exception as exc:
            logger.exception("communicate() raised: %s", exc)
            return None

        deadline = time.monotonic() + 600
        while task is not None and task.is_alive():
            if time.monotonic() > deadline:
                logger.warning(
                    "monologue for %s/%s exceeded deadline; abandoning reply capture",
                    msg.channel_type,
                    msg.chat_id,
                )
                return None
            await asyncio.sleep(0.25)

        # DeferredTask.result() is async — await it (was being discarded
        # as a coroutine and producing a RuntimeWarning; the agent's reply
        # was never picked up).
        result: Any = None
        try:
            if task is not None:
                result = await task.result()
        except Exception:
            result = None
        if isinstance(result, str) and result.strip():
            return result
        return self._extract_last_response(context)

    def _extract_last_response(self, context: "AgentContext") -> Optional[str]:
        """Best-effort: pull the last ``response``-typed log entry."""

        log = getattr(context, "log", None)
        logs = getattr(log, "logs", None) or []
        for item in reversed(logs):
            kind = getattr(item, "type", None)
            if kind in {"response", "agent"}:
                text = getattr(item, "content", None) or getattr(item, "heading", None)
                if isinstance(text, str) and text.strip():
                    return text
        return None

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def _send_reply(
        self,
        inbound: InboundMessage,
        text: str,
        *,
        reply_to: Optional[DeliveryAddress] = None,
    ) -> None:
        """Send the agent's reply.

        Spec 06 D1: if ``reply_to`` is provided (admin-transport path),
        deliver to that address — possibly a different channel — and
        skip the original inbound's metadata-based reply-threading.

        Spec 09 D5: adapter lookup uses ``(channel_type, bot_name)``.
        For an inbound reply, ``bot_name`` comes from the inbound;
        for a ``reply_to`` redirect, we pick the first adapter
        registered under the target channel_type (any bot of that
        platform can deliver the message — the redirect target is
        operator intent, not bot-specific).
        """

        if reply_to is not None:
            target_channel = self._pick_adapter_for_channel_type(reply_to.channel_type)
            if target_channel is None:
                logger.error(
                    "reply_to channel %s has no adapter; falling back to inbound",
                    reply_to.channel_type,
                )
            else:
                outbound = OutboundMessage(
                    chat_id=reply_to.platform_id,
                    text=text,
                    reply_to=reply_to.thread_id,
                    metadata={
                        "formatted": format_for_channel(text, reply_to.channel_type)
                    },
                    bot_name=target_channel.bot_name,
                )
                try:
                    await target_channel.send(outbound)
                    return
                except Exception:
                    logger.exception(
                        "reply_to send failed (%s/%s)",
                        reply_to.channel_type,
                        reply_to.platform_id,
                    )

        channel = self.channels.get((inbound.channel_type, inbound.bot_name))
        if channel is None:
            logger.error(
                "no adapter for channel %s/%s; dropping reply",
                inbound.channel_type,
                inbound.bot_name,
            )
            return
        # Carry the inbound's metadata through to the outbound so
        # platform-specific routing fields (Slack ``channel``, ``ts``,
        # ``thread_ts``; Telegram ``message_id``; Discord ``guild_id`` /
        # ``channel_id``) reach the adapter's ``send``. Then layer
        # ``formatted`` on top so the formatter's pre-built payload
        # survives.
        reply_metadata = dict(inbound.metadata or {})
        reply_metadata["formatted"] = format_for_channel(text, inbound.channel_type)
        outbound = OutboundMessage(
            chat_id=inbound.chat_id,
            text=text,
            reply_to=inbound.metadata.get("message_id"),
            metadata=reply_metadata,
            bot_name=inbound.bot_name,
        )
        try:
            await channel.send(outbound)
        except Exception:
            logger.exception(
                "send() failed for channel %s/%s chat %s",
                inbound.channel_type,
                inbound.bot_name,
                inbound.chat_id,
            )

    def _pick_adapter_for_channel_type(
        self, channel_type: str
    ) -> Optional[BaseChannel]:
        """Return any adapter registered for ``channel_type``.

        Used by the ``reply_to`` redirect path where the operator names
        a channel but not a specific bot — we deliver via whichever
        bot is available. Iteration order is insertion order (dict
        guarantee since Python 3.7), which is the order the lifecycle
        loaded bots in.
        """

        for (ct, _bot_name), adapter in self.channels.items():
            if ct == channel_type:
                return adapter
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` iff it's awaitable, otherwise return it directly."""

    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value


def _normalize_channel_key(key: Any) -> tuple[str, str]:
    """Coerce a legacy ``str`` key to ``(channel_type, "_legacy")``.

    Spec 09 D5 keys the router's adapter / config dicts by
    ``(channel_type, bot_name)``. Callers that predate the multi-bot
    wiring pass plain strings; we wrap them under the ``"_legacy"``
    bot identity so they keep working unchanged.
    """

    if isinstance(key, tuple):
        if len(key) != 2:
            raise ValueError(
                f"channel key must be (channel_type, bot_name); got {key!r}"
            )
        return (str(key[0]), str(key[1]))
    if isinstance(key, str):
        return (key, "_legacy")
    raise TypeError(f"channel key must be str or 2-tuple; got {type(key).__name__}")
