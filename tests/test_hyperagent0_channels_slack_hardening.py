"""Tests for spec 08 D9 Slack adapter hardening.

Covers:
* Event-ID LRU dedup (drops duplicate redeliveries within the cap).
* Own-bot filter via ``bot_id`` (and user-id fallback when bot_id absent).

These exercise the adapter's helper methods directly so no Socket Mode
boot is needed.
"""

from __future__ import annotations

import pytest

from hyperagent0.channels.slack import SlackChannel, _SEEN_EVENT_ID_CAP


def _make_channel() -> SlackChannel:
    return SlackChannel({"enabled": True, "token": "x", "app_token": "y"})


# ---------------------------------------------------------------------------
# Event-ID dedup
# ---------------------------------------------------------------------------


def test_dedup_first_seen_is_not_duplicate():
    ch = _make_channel()
    assert ch._is_duplicate_event("evt-1") is False


def test_dedup_repeated_id_is_duplicate():
    ch = _make_channel()
    ch._is_duplicate_event("evt-1")
    assert ch._is_duplicate_event("evt-1") is True


def test_dedup_independent_ids():
    ch = _make_channel()
    assert ch._is_duplicate_event("evt-A") is False
    assert ch._is_duplicate_event("evt-B") is False
    assert ch._is_duplicate_event("evt-A") is True


def test_dedup_none_event_id_never_duplicate():
    ch = _make_channel()
    assert ch._is_duplicate_event(None) is False
    assert ch._is_duplicate_event(None) is False
    assert ch._is_duplicate_event("") is False  # empty string also a no-op


def test_dedup_lru_cap_holds():
    """Once we overflow the cap, the oldest IDs are evicted."""

    ch = _make_channel()
    for i in range(_SEEN_EVENT_ID_CAP + 100):
        ch._is_duplicate_event(f"evt-{i}")
    assert len(ch._seen_event_ids) == _SEEN_EVENT_ID_CAP
    # The very oldest IDs are gone; a re-seen old one looks fresh.
    assert ch._is_duplicate_event("evt-0") is False


def test_dedup_lru_touch_extends_lifetime():
    """A re-seen id should move to the LRU's tail, surviving a small overflow.

    The invariant under test: of two items A (untouched) and B (touched),
    after filling to capacity and then adding one more, A is evicted
    while B survives. Without the move-to-end on re-see, A would
    survive instead.
    """

    ch = _make_channel()
    ch._is_duplicate_event("evt-A")
    ch._is_duplicate_event("evt-B")
    # Fill to exactly capacity. A and B are now the two oldest.
    for i in range(_SEEN_EVENT_ID_CAP - 2):
        ch._is_duplicate_event(f"evt-fill-{i}")
    assert len(ch._seen_event_ids) == _SEEN_EVENT_ID_CAP
    # Touching B moves it to the tail.
    assert ch._is_duplicate_event("evt-B") is True
    # Add one more item — A (now the oldest) is evicted, B survives.
    ch._is_duplicate_event("evt-new")
    assert "evt-A" not in ch._seen_event_ids
    assert "evt-B" in ch._seen_event_ids


# ---------------------------------------------------------------------------
# Own-bot filter
# ---------------------------------------------------------------------------


def test_own_bot_filter_with_bot_id_match():
    ch = _make_channel()
    ch._bot_id = "B_OWN"
    assert ch._is_own_message({"bot_id": "B_OWN", "text": "hi"}) is True


def test_own_bot_filter_with_bot_id_mismatch():
    ch = _make_channel()
    ch._bot_id = "B_OWN"
    assert ch._is_own_message({"bot_id": "B_OTHER_BOT", "text": "hi"}) is False


def test_own_bot_filter_no_bot_id_field():
    """A real-user message has no bot_id at all; that's not own."""

    ch = _make_channel()
    ch._bot_id = "B_OWN"
    assert ch._is_own_message({"user": "USOMEBODY", "text": "hi"}) is False


def test_own_bot_filter_fallback_to_user_id_when_no_bot_id_resolved():
    """When auth.test didn't return a bot_id, we fall back to user_id."""

    ch = _make_channel()
    ch._bot_id = None
    ch._bot_user_id = "UBOT"
    assert ch._is_own_message({"user": "UBOT", "text": "hi"}) is True
    assert ch._is_own_message({"user": "UOTHER", "text": "hi"}) is False


def test_own_bot_filter_no_identity_resolved_drops_nothing():
    """If neither bot_id nor user_id is known, never claim a message is ours."""

    ch = _make_channel()
    ch._bot_id = None
    ch._bot_user_id = None
    assert ch._is_own_message({"user": "UANY", "bot_id": "BANY"}) is False
