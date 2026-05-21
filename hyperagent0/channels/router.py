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
   creates a fresh one (activating the channel-bound project; applying
   the channel's ``sandbox_override`` per spec 06 D5).
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


class ThreadStore:
    """SQLite wrapper for the (channel, chat) → context mapping.

    Spec 06: schema management moved into :class:`Migrator` (numbered
    .sql files in ``migrations/``). Construction applies any pending
    migrations idempotently.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.path = db_path or default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        Migrator(self._conn, lock=self._lock).upgrade()

    def get(self, channel_type: str, chat_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT channel_type, chat_id, context_id, project_name, last_active "
                "FROM thread_map WHERE channel_type = ? AND chat_id = ?",
                (channel_type, chat_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "channel_type": row[0],
            "chat_id": row[1],
            "context_id": row[2],
            "project_name": row[3],
            "last_active": row[4],
        }

    def upsert(
        self,
        channel_type: str,
        chat_id: str,
        context_id: str,
        project_name: Optional[str] = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO thread_map(channel_type, chat_id, context_id, project_name, last_active)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_type, chat_id) DO UPDATE SET
                    context_id   = excluded.context_id,
                    project_name = excluded.project_name,
                    last_active  = excluded.last_active
                """,
                (channel_type, chat_id, context_id, project_name, now),
            )
            self._conn.commit()

    def touch(self, channel_type: str, chat_id: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE thread_map SET last_active = ? "
                "WHERE channel_type = ? AND chat_id = ?",
                (now, channel_type, chat_id),
            )
            self._conn.commit()

    def all_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT channel_type, chat_id, context_id, project_name, last_active "
                "FROM thread_map ORDER BY last_active DESC"
            )
            rows = cur.fetchall()
        return [
            {
                "channel_type": r[0],
                "chat_id": r[1],
                "context_id": r[2],
                "project_name": r[3],
                "last_active": r[4],
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
        channels: Optional[Dict[str, BaseChannel]] = None,
        channel_configs: Optional[Dict[str, ChannelConfig]] = None,
        store: Optional[ThreadStore] = None,
        reply_factory: Optional[ReplyFactory] = None,
    ) -> None:
        self.channels: Dict[str, BaseChannel] = dict(channels or {})
        self.channel_configs: Dict[str, ChannelConfig] = dict(channel_configs or {})
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
        """

        self.channels[channel.channel_type] = channel
        if config is not None:
            self.channel_configs[channel.channel_type] = config
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
        # We don't know the channel_type here without context; the
        # touch is a no-op when no row exists.
        if not platform_id:
            return
        for ct in list(self.channels.keys()):
            self.store.touch(ct, platform_id)

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
        cfg = self.channel_configs.get(msg.channel_type)
        if cfg is not None:
            if not cfg.is_user_allowed(msg.user_id):
                logger.info(
                    "channel %s rejected user %s (not in allow-list)",
                    msg.channel_type,
                    msg.user_id,
                )
                return
            if not cfg.is_chat_allowed(msg.chat_id):
                logger.info(
                    "channel %s rejected chat %s (not in allow-list)",
                    msg.channel_type,
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
                    "channel %s chat %s: group msg without mention; ignored",
                    msg.channel_type,
                    msg.chat_id,
                )
                return

        existing = self.store.get(msg.channel_type, msg.chat_id)
        context_id_hint = existing["context_id"] if existing else None

        # If a test reply factory is set, take that path and skip the
        # AgentContext machinery entirely.
        if self.reply_factory is not None:
            try:
                reply = await _maybe_await(
                    self.reply_factory(msg, self.channels[msg.channel_type], context_id_hint)
                )
            except KeyError:
                logger.error(
                    "no adapter registered for channel %s", msg.channel_type
                )
                return
            if reply:
                await self._send_reply(msg, str(reply), reply_to=reply_to)
            self.store.upsert(
                msg.channel_type, msg.chat_id, context_id_hint or "test", None
            )
            return

        context, project_name = self._get_or_create_context(msg, cfg, existing)
        if context is None:
            logger.error(
                "could not obtain AgentContext for %s/%s",
                msg.channel_type,
                msg.chat_id,
            )
            return

        # Persist the (possibly new) mapping before dispatch so a crash
        # mid-monologue still leaves the chat resumable.
        self.store.upsert(
            msg.channel_type, msg.chat_id, context.id, project_name
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
        """Return (context, project_name). Imports agent lazily.

        Spec 06 D5: after project activation, apply
        ``cfg.sandbox_override`` to the new context's AgentConfig so
        the code-execution path picks it up.
        """

        try:
            from agent import AgentContext  # type: ignore
        except Exception as exc:
            logger.error("agent module not importable: %s", exc)
            return None, None

        # Try to resume.
        if existing:
            ctx = AgentContext.get(existing["context_id"])
            if ctx is not None:
                return ctx, existing.get("project_name")

        # Fresh context. Use the global AgentConfig the daemon already
        # built — same pattern run_ui uses for new web UI sessions.
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

        # Spec 06 D5: apply channel-level sandbox override AFTER project
        # activation, so it wins regardless of project defaults.
        if cfg is not None and cfg.sandbox_override is not None:
            self._apply_sandbox_override(ctx, cfg.sandbox_override)

        return ctx, project_name

    def _apply_sandbox_override(
        self, ctx: "AgentContext", override: Dict[str, Any]
    ) -> None:
        mode = override.get("mode")
        if not mode or mode == "inherit":
            return
        cfg = getattr(ctx, "config", None)
        if cfg is None:
            return
        # Spec 01 stashes sandbox_mode in AgentConfig.additional to keep
        # agent.py off the patch list. Same pattern here.
        additional = getattr(cfg, "additional", None)
        if isinstance(additional, dict):
            additional["sandbox_mode"] = mode
            logger.info(
                "channel sandbox override applied: %s for context %s",
                mode,
                getattr(ctx, "id", "?"),
            )

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

        result = None
        try:
            result = task.result() if task is not None else None  # type: ignore[union-attr]
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
        """

        if reply_to is not None:
            target_channel = self.channels.get(reply_to.channel_type)
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

        channel = self.channels.get(inbound.channel_type)
        if channel is None:
            logger.error(
                "no adapter for channel %s; dropping reply", inbound.channel_type
            )
            return
        outbound = OutboundMessage(
            chat_id=inbound.chat_id,
            text=text,
            reply_to=inbound.metadata.get("message_id"),
            metadata={"formatted": format_for_channel(text, inbound.channel_type)},
        )
        try:
            await channel.send(outbound)
        except Exception:
            logger.exception(
                "send() failed for channel %s chat %s",
                inbound.channel_type,
                inbound.chat_id,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` iff it's awaitable, otherwise return it directly."""

    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value
