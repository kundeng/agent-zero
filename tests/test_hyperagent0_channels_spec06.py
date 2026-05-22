"""Tests for spec 06 channel-hardening additions.

Covers:
* Migration system runs idempotently and tracks applied versions.
* `is_mention` enforcement via `require_mention` config.
* `reply_to` (DeliveryAddress) routing — inbound on channel A, reply
  delivered on channel B.
* `is_network_error` discrimination (true positives for stdlib transients,
  false for misconfigs).
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from hyperagent0.channels.base import (
    BaseChannel,
    DeliveryAddress,
    InboundEvent,
    InboundMessage,
    OutboundMessage,
)
from hyperagent0.channels.config import ChannelConfig
from hyperagent0.channels.lifecycle import is_network_error
from hyperagent0.channels.migrations.migrator import Migrator
from hyperagent0.channels.router import ChannelRouter, ThreadStore


# ---------------------------------------------------------------------------
# Migration system
# ---------------------------------------------------------------------------


def test_migrator_applies_initial_then_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "ch.db"
    conn = sqlite3.connect(str(db))
    m = Migrator(conn)
    first_run = m.upgrade()
    assert 1 in first_run
    # Second invocation must be a no-op.
    second_run = m.upgrade()
    assert second_run == []
    # thread_map must exist (the only object 001 creates).
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='thread_map'"
    )
    assert cur.fetchone() is not None
    conn.close()


def test_thread_store_uses_migrator(tmp_path: Path) -> None:
    """Fresh ThreadStore must be functional + records its applied versions."""

    store = ThreadStore(db_path=tmp_path / "ch.db")
    store.upsert("test", "chat1", "ctx1")
    assert store.get("test", "chat1") is not None

    # Re-open the same DB; migrator must see no pending work.
    store2 = ThreadStore(db_path=tmp_path / "ch.db")
    rows = store2.all_rows()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# is_mention + require_mention enforcement
# ---------------------------------------------------------------------------


class _StubChannel(BaseChannel):
    """Minimal in-memory adapter for routing tests."""

    channel_type = "stub"

    def __init__(self, name: str = "stub") -> None:
        super().__init__({})
        self.channel_type = name
        self.sent: list[OutboundMessage] = []

    async def connect(self) -> None:  # pragma: no cover - unused
        return None

    async def disconnect(self) -> None:  # pragma: no cover - unused
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


@pytest.fixture()
def isolated_router(tmp_path: Path) -> ChannelRouter:
    return ChannelRouter(store=ThreadStore(db_path=tmp_path / "ch.db"))


@pytest.mark.asyncio
async def test_require_mention_blocks_unmentioned_group(
    isolated_router: ChannelRouter,
) -> None:
    ch = _StubChannel("stub")
    cfg = ChannelConfig(name="stub", enabled=True, require_mention=True)
    isolated_router.register(ch, cfg)
    isolated_router.reply_factory = lambda *_: "should not be called"

    # Group message without a mention — must be ignored.
    msg = InboundMessage(
        channel_type="stub",
        chat_id="g1",
        user_id="u1",
        text="hi everyone",
        is_group=True,
        is_mention=False,
    )
    await isolated_router.handle_inbound(msg)
    assert ch.sent == []


@pytest.mark.asyncio
async def test_require_mention_passes_mentioned_group(
    isolated_router: ChannelRouter,
) -> None:
    ch = _StubChannel("stub")
    cfg = ChannelConfig(name="stub", enabled=True, require_mention=True)
    isolated_router.register(ch, cfg)
    isolated_router.reply_factory = lambda inbound, _channel, _hint: "ack"

    msg = InboundMessage(
        channel_type="stub",
        chat_id="g1",
        user_id="u1",
        text="@bot do it",
        is_group=True,
        is_mention=True,
    )
    await isolated_router.handle_inbound(msg)
    assert len(ch.sent) == 1
    assert ch.sent[0].text == "ack"


@pytest.mark.asyncio
async def test_require_mention_does_not_block_dms(
    isolated_router: ChannelRouter,
) -> None:
    ch = _StubChannel("stub")
    cfg = ChannelConfig(name="stub", enabled=True, require_mention=True)
    isolated_router.register(ch, cfg)
    isolated_router.reply_factory = lambda inbound, _channel, _hint: "ack"

    msg = InboundMessage(
        channel_type="stub",
        chat_id="dm1",
        user_id="u1",
        text="hi",
        is_group=False,
        is_mention=False,
    )
    await isolated_router.handle_inbound(msg)
    assert len(ch.sent) == 1


# ---------------------------------------------------------------------------
# reply_to routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_to_redirects_to_different_adapter(
    isolated_router: ChannelRouter,
) -> None:
    inbound_ch = _StubChannel("inboundchan")
    reply_ch = _StubChannel("replychan")
    isolated_router.register(inbound_ch)
    isolated_router.register(reply_ch)
    isolated_router.reply_factory = lambda *_: "echo"

    msg = InboundMessage(
        channel_type="inboundchan",
        chat_id="srcchat",
        user_id="u1",
        text="ping",
    )
    event = InboundEvent(
        channel_type="inboundchan",
        platform_id="srcchat",
        thread_id=None,
        message=msg,
        reply_to=DeliveryAddress(
            channel_type="replychan",
            platform_id="dstchat",
            thread_id="thr1",
        ),
    )

    await isolated_router.on_inbound_event(event)

    # Reply MUST land on replychan, not the inbound channel.
    assert inbound_ch.sent == []
    assert len(reply_ch.sent) == 1
    out = reply_ch.sent[0]
    assert out.chat_id == "dstchat"
    assert out.reply_to == "thr1"
    assert out.text == "echo"


@pytest.mark.asyncio
async def test_no_reply_to_lands_on_inbound_channel(
    isolated_router: ChannelRouter,
) -> None:
    ch = _StubChannel("stub")
    isolated_router.register(ch)
    isolated_router.reply_factory = lambda *_: "default-route"

    msg = InboundMessage(
        channel_type="stub",
        chat_id="c1",
        user_id="u1",
        text="ping",
    )
    await isolated_router.on_inbound(msg.chat_id, None, msg)

    assert len(ch.sent) == 1
    assert ch.sent[0].chat_id == "c1"


# ---------------------------------------------------------------------------
# is_network_error discrimination
# ---------------------------------------------------------------------------


def test_is_network_error_true_for_stdlib_transients() -> None:
    import asyncio as _asyncio
    import socket as _socket

    assert is_network_error(ConnectionResetError("reset"))
    assert is_network_error(ConnectionRefusedError("refused"))
    assert is_network_error(TimeoutError("slow"))
    assert is_network_error(_asyncio.TimeoutError())
    assert is_network_error(_socket.gaierror("dns fail"))


def test_is_network_error_false_for_misconfigs() -> None:
    # Bad token / missing field → ValueError, TypeError, etc. Must NOT
    # be retried.
    assert is_network_error(ValueError("bad token")) is False
    assert is_network_error(TypeError("wrong shape")) is False
    assert is_network_error(KeyError("missing")) is False
    assert is_network_error(RuntimeError("misconfig")) is False


# ---------------------------------------------------------------------------
# ChannelSetup wire-up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_path_preferred_over_legacy_on_message(
    isolated_router: ChannelRouter,
) -> None:
    """When both setup() and on_message are wired, the new path wins."""

    ch = _StubChannel("stub")
    isolated_router.register(ch)

    # Both should be set by register():
    assert ch.channel_setup is not None
    assert ch.on_message is not None

    isolated_router.reply_factory = lambda *_: "ok"
    msg = InboundMessage(
        channel_type="stub", chat_id="c1", user_id="u1", text="hi"
    )
    # Adapter dispatch routes through the spec-06 ChannelSetup.
    await ch._dispatch_inbound(msg)
    assert len(ch.sent) == 1
