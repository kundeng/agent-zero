"""End-to-end Telegram channel test (spec 04, task 2.3) — fully mocked.

We do not hit the Telegram Bot API. Instead, the test:
  * builds a :class:`TelegramChannel` with a stubbed config,
  * patches its internal SDK references with a fake bot client,
  * feeds the adapter a synthetic inbound (the same shape the
    python-telegram-bot ``MessageHandler`` would produce),
  * routes the message through :class:`ChannelRouter` with a fake
    reply factory in lieu of a real :class:`agent.AgentContext`,
  * verifies that the reply round-trips out through ``TelegramChannel.send``
    as HTML-formatted chunks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hyperagent0.channels.base import InboundMessage, OutboundMessage
from hyperagent0.channels.config import ChannelConfig
from hyperagent0.channels.router import ChannelRouter, ThreadStore
from hyperagent0.channels.telegram import TelegramChannel


class _FakeBot:
    """Stand-in for ``telegram.Bot`` — records ``send_message`` calls."""

    def __init__(self):
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)


def test_telegram_end_to_end_with_mocked_bot(tmp_path: Path):
    # Build the adapter and inject a fake bot so ``send()`` doesn't try
    # to reach Telegram. ``connect()`` is not called — we only exercise
    # the inbound dispatch + outbound send paths.
    adapter = TelegramChannel({"token": "fake-token-not-resolved"})
    fake_bot = _FakeBot()
    adapter._bot = fake_bot  # type: ignore[attr-defined]

    store = ThreadStore(db_path=tmp_path / "channels.db")
    cfg = ChannelConfig(name="telegram")

    async def reply_factory(msg: InboundMessage, ch, _ctx_hint):
        # Pretend the agent responded with mixed-format markdown.
        return f"**Hi {msg.user_name}**, you said: `{msg.text}`"

    router = ChannelRouter(
        channel_configs={"telegram": cfg},
        store=store,
        reply_factory=reply_factory,
    )
    router.register(adapter, cfg)

    # Synthesize the inbound the way the real Telegram MessageHandler would.
    inbound = InboundMessage(
        channel_type="telegram",
        chat_id="55555",
        user_id="42",
        user_name="alice",
        text="hello bot",
        metadata={"message_id": 1001, "chat_type": "private"},
    )

    asyncio.run(router.handle_inbound(inbound))

    # The router should have called the adapter's ``send`` once.
    assert len(fake_bot.calls) == 1
    call = fake_bot.calls[0]
    assert call["chat_id"] == "55555"
    assert call["parse_mode"] == "HTML"
    assert call["reply_to_message_id"] == 1001
    # The text should have the markdown applied as Telegram HTML.
    assert "<b>Hi alice</b>" in call["text"]
    assert "<code>hello bot</code>" in call["text"]

    # The mapping should be persisted for future resume.
    row = store.get("telegram", "55555")
    assert row is not None

    store.close()


def test_telegram_send_skips_chunks_when_disconnected(tmp_path: Path):
    """``send()`` with no bot must not raise (graceful no-op)."""

    adapter = TelegramChannel({"token": "x"})
    # _bot stays None — adapter is "disconnected".
    asyncio.run(
        adapter.send(OutboundMessage(chat_id="1", text="should drop silently"))
    )
    # No assertion needed — passing without raising is the contract.
