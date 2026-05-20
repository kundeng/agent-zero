"""Channel → AgentContext router (spec 04, task 1.2).

Inbound messages from any channel arrive at
:meth:`ChannelRouter.handle_inbound`. The router:

1. Validates the sender against the channel's allow-list.
2. Looks up the persistent ``(channel_type, chat_id) → context_id``
   mapping in SQLite (``~/.hyperagent0/channels.db``).
3. Resumes the existing :class:`agent.AgentContext` if one is live, or
   creates a fresh one (activating the channel-bound project if the
   channel config has one).
4. Dispatches the message via ``context.communicate(...)``.
5. After the agent finishes, ships the reply back via the originating
   channel adapter.

Persistence design
------------------
The SQLite schema is intentionally tiny:

.. code-block:: sql

   CREATE TABLE thread_map (
     channel_type TEXT NOT NULL,
     chat_id      TEXT NOT NULL,
     context_id   TEXT NOT NULL,
     project_name TEXT,
     last_active  REAL NOT NULL,
     PRIMARY KEY (channel_type, chat_id)
   );

Only the durable mapping lives here; full conversation state stays in
the agent's normal persistence layer (``persist_chat``). On daemon
restart, looking up an old ``context_id`` may return a stale
identifier whose :class:`AgentContext` is gone — we treat that as
"create new" rather than try to rehydrate the chat log. That trade-off
is called out as an open question in the spec; full resume lands with
P2 task 2.5.
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

from .base import BaseChannel, InboundMessage, OutboundMessage
from .config import ChannelConfig
from .formatter import format_for_channel

if TYPE_CHECKING:  # pragma: no cover - import-cycle guard
    from agent import AgentContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQLite-backed mapping store
# ---------------------------------------------------------------------------


def default_db_path() -> Path:
    return Path(os.path.expanduser("~/.hyperagent0/channels.db"))


class ThreadStore:
    """Thin SQLite wrapper for the (channel, chat) → context mapping.

    The instance owns a single connection guarded by a lock so it can be
    used from multiple adapter coroutines safely (SQLite is happy with
    serialized access across threads as long as the connection is
    shared with ``check_same_thread=False``).
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.path = db_path or default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS thread_map (
                    channel_type TEXT NOT NULL,
                    chat_id      TEXT NOT NULL,
                    context_id   TEXT NOT NULL,
                    project_name TEXT,
                    last_active  REAL NOT NULL,
                    PRIMARY KEY (channel_type, chat_id)
                );
                CREATE INDEX IF NOT EXISTS idx_thread_context
                    ON thread_map(context_id);
                """
            )
            self._conn.commit()

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


class ChannelRouter:
    """Routes inbound channel messages to AgentContexts and back.

    The router is created once by the daemon. Each adapter sets the
    router's bound callback as its ``on_message`` handler before
    :meth:`BaseChannel.connect` returns.
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
        """Attach a channel adapter. Wires up ``on_message``."""

        self.channels[channel.channel_type] = channel
        if config is not None:
            self.channel_configs[channel.channel_type] = config
        channel.on_message = self.handle_inbound

    # ------------------------------------------------------------------
    # Inbound handling
    # ------------------------------------------------------------------

    async def handle_inbound(self, msg: InboundMessage) -> None:
        """Process a single inbound message end-to-end."""

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
                await self._send_reply(msg, str(reply))
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
            await self._send_reply(msg, reply_text)

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

        return ctx, project_name

    async def _dispatch_to_context(
        self, context: "AgentContext", msg: InboundMessage
    ) -> Optional[str]:
        """Hand the inbound to the agent and wait for the monologue to finish.

        Returns the agent's final reply text (or ``None`` if we couldn't
        capture one). We use ``context.communicate`` which schedules
        the monologue on the context's ``DeferredTask``; we then poll
        the task until it finishes, with a generous ceiling so a long
        agent run doesn't block the adapter loop forever.
        """

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

        # Wait for the monologue to finish without blocking the event
        # loop. Cap at 10 minutes — agents that take longer are
        # expected to send intermediate replies via the proactive-send
        # path (open question 3 in the spec).
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

        # The agent's final answer is stored on the task result. We
        # fall back to scanning the log for the most recent ``response``
        # entry if the task did not return a string directly.
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

    async def _send_reply(self, inbound: InboundMessage, text: str) -> None:
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
