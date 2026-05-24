"""Tests for spec 09 multi-bot foundation.

Covers:

* Migration 002 lifts the (channel_type, chat_id) PK to
  (channel_type, bot_name, chat_id) while preserving existing rows
  with ``bot_name='_legacy'`` (so single-bot installs keep working).
* Migrator records version 2 and is idempotent on re-run.
* :class:`ThreadStore` disambiguates two bots sharing the same
  channel_type + chat_id — the spec-09 D3 use case.
* :class:`BaseChannel` carries a ``bot_name`` attribute populated at
  construction (spec 09 task 1.7).
* :class:`ChannelRouter` indexes adapters by ``(channel_type,
  bot_name)`` so two bots on the same platform dispatch independently.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from hyperagent0.channels.base import (
    BaseChannel,
    InboundMessage,
    OutboundMessage,
)
from hyperagent0.channels.config import ChannelConfig
from hyperagent0.channels.migrations.migrator import Migrator
from hyperagent0.channels.router import (
    LEGACY_BOT_NAME,
    ChannelRouter,
    ThreadStore,
)


# ---------------------------------------------------------------------------
# Migration 002
# ---------------------------------------------------------------------------


def _seed_v1_thread_map(db_path: Path) -> None:
    """Lay down the spec-04 schema by hand so we can verify the v1→v2 jump."""

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE thread_map (
                channel_type TEXT NOT NULL,
                chat_id      TEXT NOT NULL,
                context_id   TEXT NOT NULL,
                project_name TEXT,
                last_active  REAL NOT NULL,
                PRIMARY KEY (channel_type, chat_id)
            );
            CREATE INDEX IF NOT EXISTS idx_thread_context
                ON thread_map(context_id);
            CREATE TABLE schema_migrations (
                version    INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );
            INSERT INTO schema_migrations(version, applied_at) VALUES (1, 0.0);
            INSERT INTO thread_map(channel_type, chat_id, context_id, project_name, last_active)
                VALUES ('slack', 'C001', 'ctx-old', 'engineering', 123.0);
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_migration_002_upgrades_v1_db(tmp_path: Path) -> None:
    db = tmp_path / "ch.db"
    _seed_v1_thread_map(db)

    conn = sqlite3.connect(str(db))
    applied = Migrator(conn).upgrade()
    assert 2 in applied, "002_bot_name.sql must apply when v1 db exists"

    # Existing row preserved with bot_name='_legacy'.
    row = conn.execute(
        "SELECT channel_type, bot_name, chat_id, context_id, project_name "
        "FROM thread_map WHERE chat_id = 'C001'"
    ).fetchone()
    assert row == ("slack", LEGACY_BOT_NAME, "C001", "ctx-old", "engineering")

    # PK is now the 3-tuple — inserting (slack, '_legacy', 'C001', ...) again must conflict;
    # inserting under a different bot_name must succeed.
    conn.execute(
        "INSERT INTO thread_map(channel_type, bot_name, chat_id, context_id, project_name, last_active) "
        "VALUES ('slack', 'hazbot', 'C001', 'ctx-new', 'customer-ops', 124.0)"
    )
    conn.commit()

    rows = conn.execute(
        "SELECT bot_name, context_id FROM thread_map WHERE channel_type='slack' AND chat_id='C001' ORDER BY bot_name"
    ).fetchall()
    assert rows == [("_legacy", "ctx-old"), ("hazbot", "ctx-new")]
    conn.close()


def test_migration_002_idempotent_on_fresh_db(tmp_path: Path) -> None:
    """A brand-new ThreadStore boots straight onto v2 — both migrations apply once."""

    store = ThreadStore(db_path=tmp_path / "ch.db")
    try:
        # Re-opening the same DB must not re-apply anything.
        store2 = ThreadStore(db_path=tmp_path / "ch.db")
        cur = store2._conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
        versions = [r[0] for r in cur.fetchall()]
        assert versions == [1, 2]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# ThreadStore bot_name disambiguation (spec 09 D3)
# ---------------------------------------------------------------------------


def test_thread_store_disambiguates_by_bot_name(tmp_path: Path) -> None:
    store = ThreadStore(db_path=tmp_path / "ch.db")
    try:
        # Same channel_type + chat_id, two different bots.
        store.upsert("slack", "C001", "ctx-A", "engineering", bot_name="hazbot")
        store.upsert("slack", "C001", "ctx-B", "customer-ops", bot_name="support-bot")

        a = store.get("slack", "C001", bot_name="hazbot")
        b = store.get("slack", "C001", bot_name="support-bot")
        assert a is not None and a["context_id"] == "ctx-A"
        assert b is not None and b["context_id"] == "ctx-B"
        assert a["project_name"] == "engineering"
        assert b["project_name"] == "customer-ops"

        # Default-bot_name caller (no kwarg) gets neither — they live under '_legacy'.
        assert store.get("slack", "C001") is None
    finally:
        store.close()


def test_thread_store_touch_scoped_to_bot_name(tmp_path: Path) -> None:
    store = ThreadStore(db_path=tmp_path / "ch.db")
    try:
        store.upsert("slack", "C001", "ctx-A", bot_name="hazbot")
        store.upsert("slack", "C001", "ctx-B", bot_name="support-bot")
        before_a = store.get("slack", "C001", bot_name="hazbot")["last_active"]
        before_b = store.get("slack", "C001", bot_name="support-bot")["last_active"]

        # Touching one must not bump the other.
        import time as _t

        _t.sleep(0.01)
        store.touch("slack", "C001", bot_name="hazbot")
        after_a = store.get("slack", "C001", bot_name="hazbot")["last_active"]
        after_b = store.get("slack", "C001", bot_name="support-bot")["last_active"]
        assert after_a > before_a
        assert after_b == before_b
    finally:
        store.close()


def test_thread_store_all_rows_includes_bot_name(tmp_path: Path) -> None:
    store = ThreadStore(db_path=tmp_path / "ch.db")
    try:
        store.upsert("slack", "C001", "ctx-A", bot_name="hazbot")
        store.upsert("slack", "C001", "ctx-B", bot_name="support-bot")
        rows = store.all_rows()
        names = {(r["channel_type"], r["bot_name"], r["chat_id"]) for r in rows}
        assert names == {
            ("slack", "hazbot", "C001"),
            ("slack", "support-bot", "C001"),
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# BaseChannel.bot_name (spec 09 task 1.7)
# ---------------------------------------------------------------------------


class _NamedChannel(BaseChannel):
    channel_type = "named"

    async def connect(self) -> None:  # pragma: no cover - unused
        return None

    async def disconnect(self) -> None:  # pragma: no cover - unused
        return None

    async def send(self, msg: OutboundMessage) -> None:  # pragma: no cover - unused
        return None


def test_base_channel_default_bot_name_is_legacy() -> None:
    ch = _NamedChannel(config={})
    assert ch.bot_name == "_legacy"


def test_base_channel_accepts_bot_name() -> None:
    ch = _NamedChannel(config={}, bot_name="hazbot")
    assert ch.bot_name == "hazbot"


# ---------------------------------------------------------------------------
# Router routes (channel_type, bot_name) (spec 09 D5)
# ---------------------------------------------------------------------------


class _CapturingChannel(BaseChannel):
    """Adapter that records sent OutboundMessages for assertion."""

    channel_type = "slack"

    def __init__(self, bot_name: str) -> None:
        super().__init__(config={}, bot_name=bot_name)
        self.sent: list[OutboundMessage] = []

    async def connect(self) -> None:  # pragma: no cover - unused
        return None

    async def disconnect(self) -> None:  # pragma: no cover - unused
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_router_dispatches_per_bot_independently(tmp_path: Path) -> None:
    """Two bots on the same platform must route to separate adapters."""

    hazbot = _CapturingChannel("hazbot")
    support = _CapturingChannel("support-bot")

    async def factory(msg, ch, ctx_hint):
        return f"{ch.bot_name}:{msg.text}"

    router = ChannelRouter(
        store=ThreadStore(db_path=tmp_path / "ch.db"),
        reply_factory=factory,
    )
    router.register(hazbot, ChannelConfig(name="slack"))
    router.register(support, ChannelConfig(name="slack"))

    # Same channel_type + chat_id, different bot_name → different adapters.
    _run(
        router.handle_inbound(
            InboundMessage(
                channel_type="slack",
                chat_id="C001",
                user_id="u1",
                text="hi",
                bot_name="hazbot",
            )
        )
    )
    _run(
        router.handle_inbound(
            InboundMessage(
                channel_type="slack",
                chat_id="C001",
                user_id="u1",
                text="hey",
                bot_name="support-bot",
            )
        )
    )

    # Each adapter sees only its own reply.
    assert len(hazbot.sent) == 1
    assert hazbot.sent[0].text == "hazbot:hi"
    assert hazbot.sent[0].bot_name == "hazbot"
    assert len(support.sent) == 1
    assert support.sent[0].text == "support-bot:hey"
    assert support.sent[0].bot_name == "support-bot"


def test_router_legacy_string_keys_normalized(tmp_path: Path) -> None:
    """Pre-spec-09 callers pass plain str keys; router auto-wraps to (str, '_legacy')."""

    ch = _CapturingChannel("_legacy")
    router = ChannelRouter(
        channels={"slack": ch},  # legacy single-string key
        channel_configs={"slack": ChannelConfig(name="slack")},
        store=ThreadStore(db_path=tmp_path / "ch.db"),
        reply_factory=lambda msg, c, h: "ok",
    )

    # Adapter still findable under the normalized key.
    assert router.channels.get(("slack", "_legacy")) is ch
    assert router.channel_configs.get(("slack", "_legacy")) is not None

    # Inbound with default bot_name (=_legacy) reaches the adapter.
    _run(
        router.handle_inbound(
            InboundMessage(
                channel_type="slack",
                chat_id="C002",
                user_id="u",
                text="ping",
            )
        )
    )
    assert ch.sent and ch.sent[0].text == "ok"
