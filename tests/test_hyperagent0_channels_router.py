"""Tests for ``hyperagent0.channels.router``.

Covers the SQLite-backed thread store and the router's allow-list /
project-binding logic. Uses the router's ``reply_factory`` test seam so
no real AgentContext is required.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Ensure the repo root is importable before hyperagent0 is loaded by
# the test collector. (Pytest typically does this via conftest, but we
# guard explicitly to be robust against direct ``pytest <file>`` runs.)
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hyperagent0.channels.base import (
    BaseChannel,
    InboundMessage,
    OutboundMessage,
)
from hyperagent0.channels.config import ChannelConfig
from hyperagent0.channels.router import ChannelRouter, ThreadStore


# ---------------------------------------------------------------------------
# Fake channel adapter
# ---------------------------------------------------------------------------


class _FakeChannel(BaseChannel):
    channel_type = "fake"

    def __init__(self):
        super().__init__(config={})
        self.sent: list[OutboundMessage] = []
        self.connected = False
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


# ---------------------------------------------------------------------------
# ThreadStore
# ---------------------------------------------------------------------------


def test_thread_store_upsert_and_get(tmp_path: Path):
    store = ThreadStore(db_path=tmp_path / "channels.db")
    assert store.get("fake", "chat-1") is None

    store.upsert("fake", "chat-1", "ctx-A", project_name="proj-x")
    row = store.get("fake", "chat-1")
    assert row is not None
    assert row["context_id"] == "ctx-A"
    assert row["project_name"] == "proj-x"
    assert row["last_active"] > 0

    # Upserting again rotates context_id.
    store.upsert("fake", "chat-1", "ctx-B", project_name=None)
    row = store.get("fake", "chat-1")
    assert row["context_id"] == "ctx-B"
    assert row["project_name"] is None

    store.close()


def test_thread_store_isolates_by_channel(tmp_path: Path):
    store = ThreadStore(db_path=tmp_path / "channels.db")
    store.upsert("telegram", "1", "ctx-T", None)
    store.upsert("slack", "1", "ctx-S", None)
    assert store.get("telegram", "1")["context_id"] == "ctx-T"
    assert store.get("slack", "1")["context_id"] == "ctx-S"
    store.close()


def test_thread_store_persists_across_instances(tmp_path: Path):
    db = tmp_path / "channels.db"
    s1 = ThreadStore(db_path=db)
    s1.upsert("telegram", "abc", "ctx-1", "p")
    s1.close()

    s2 = ThreadStore(db_path=db)
    row = s2.get("telegram", "abc")
    assert row is not None and row["context_id"] == "ctx-1"
    assert row["project_name"] == "p"
    s2.close()


def test_thread_store_all_rows(tmp_path: Path):
    store = ThreadStore(db_path=tmp_path / "channels.db")
    store.upsert("telegram", "1", "ctx-A", None)
    store.upsert("telegram", "2", "ctx-B", None)
    rows = store.all_rows()
    assert len(rows) == 2
    ids = {r["context_id"] for r in rows}
    assert ids == {"ctx-A", "ctx-B"}
    store.close()


# ---------------------------------------------------------------------------
# ChannelConfig allow-list
# ---------------------------------------------------------------------------


def test_channel_config_allowed_users_empty_is_open():
    cfg = ChannelConfig(name="fake")
    assert cfg.is_user_allowed("anyone")
    assert cfg.is_chat_allowed("anywhere")


def test_channel_config_allow_list_enforces():
    cfg = ChannelConfig(name="fake", allowed_users=["123"], allowed_chats=["c1"])
    assert cfg.is_user_allowed("123")
    assert not cfg.is_user_allowed("999")
    assert cfg.is_chat_allowed("c1")
    assert not cfg.is_chat_allowed("c2")


def test_channel_config_project_binding_exact_then_default():
    cfg = ChannelConfig(
        name="fake",
        project_binding={"chat-1": "proj-x", "default": "proj-y"},
    )
    assert cfg.project_for_chat("chat-1") == "proj-x"
    assert cfg.project_for_chat("chat-2") == "proj-y"


def test_channel_config_project_binding_returns_none_when_empty():
    cfg = ChannelConfig(name="fake")
    assert cfg.project_for_chat("anything") is None


# ---------------------------------------------------------------------------
# Router routing via the reply_factory test seam
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_router_dispatches_and_persists_mapping(tmp_path: Path):
    channel = _FakeChannel()
    cfg = ChannelConfig(name="fake")
    store = ThreadStore(db_path=tmp_path / "channels.db")

    async def factory(msg, ch, ctx_hint):
        return f"echo:{msg.text}"

    router = ChannelRouter(
        channel_configs={"fake": cfg},
        store=store,
        reply_factory=factory,
    )
    router.register(channel, cfg)

    inbound = InboundMessage(
        channel_type="fake",
        chat_id="chat-1",
        user_id="user-1",
        text="hello",
    )
    _run(router.handle_inbound(inbound))

    # Reply went out.
    assert len(channel.sent) == 1
    assert channel.sent[0].text == "echo:hello"
    assert channel.sent[0].chat_id == "chat-1"

    # Mapping persisted.
    row = store.get("fake", "chat-1")
    assert row is not None
    store.close()


def test_router_rejects_disallowed_user(tmp_path: Path):
    channel = _FakeChannel()
    cfg = ChannelConfig(name="fake", allowed_users=["123"])
    store = ThreadStore(db_path=tmp_path / "channels.db")

    async def factory(msg, ch, ctx_hint):  # pragma: no cover - should not run
        raise AssertionError("factory called for disallowed user")

    router = ChannelRouter(
        channel_configs={"fake": cfg},
        store=store,
        reply_factory=factory,
    )
    router.register(channel, cfg)

    inbound = InboundMessage(
        channel_type="fake",
        chat_id="chat-1",
        user_id="evil",
        text="hi",
    )
    _run(router.handle_inbound(inbound))

    assert channel.sent == []
    assert store.get("fake", "chat-1") is None
    store.close()


def test_router_resume_existing_mapping(tmp_path: Path):
    channel = _FakeChannel()
    cfg = ChannelConfig(name="fake")
    store = ThreadStore(db_path=tmp_path / "channels.db")
    store.upsert("fake", "chat-1", "ctx-existing", "proj-z")

    received_hints: list = []

    async def factory(msg, ch, ctx_hint):
        received_hints.append(ctx_hint)
        return "ok"

    router = ChannelRouter(
        channel_configs={"fake": cfg},
        store=store,
        reply_factory=factory,
    )
    router.register(channel, cfg)

    inbound = InboundMessage(
        channel_type="fake", chat_id="chat-1", user_id="u", text="."
    )
    _run(router.handle_inbound(inbound))

    assert received_hints == ["ctx-existing"]
    store.close()


# ---------------------------------------------------------------------------
# Channel registry
# ---------------------------------------------------------------------------


def test_telegram_module_registers_without_pulling_sdk():
    """Importing the telegram adapter module must NOT import the SDK."""
    import sys

    before = set(sys.modules)
    import hyperagent0.channels.telegram  # noqa: F401

    after = set(sys.modules)
    forbidden = {m for m in after - before if m == "telegram" or m.startswith("telegram.")}
    assert forbidden == set(), f"telegram SDK eagerly imported: {forbidden}"

    from hyperagent0.channels.base import get_channel_class

    assert get_channel_class("telegram") is not None
