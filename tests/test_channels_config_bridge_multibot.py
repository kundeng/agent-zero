"""Tests for the bot-aware channels-config bridge (spec 09 task 1.13).

The bridge has to normalize three on-disk shapes — missing file, old
dict-shape (spec 04), new list-shape (spec 09) — into a single
list-of-bots view, then upsert by name. These tests pin down the
edge cases so future refactors don't silently break either the
strangler-fig legacy path or the new multi-bot path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperagent0.channels.channels_config_bridge import FileChannelsConfigBridge


@pytest.fixture
def bridge_path(tmp_path) -> Path:
    return tmp_path / "channels.json"


# ---------------------------------------------------------------------------
# read_bot_block
# ---------------------------------------------------------------------------


def test_read_bot_block_missing_file_returns_empty(bridge_path):
    bridge = FileChannelsConfigBridge(path=bridge_path)
    assert bridge.read_bot_block("slack", "anything") == {}


def test_read_bot_block_dict_shape_treats_as_default(bridge_path):
    """Old dict-shape entry surfaces under name='default'."""

    bridge_path.write_text(
        json.dumps({"slack": {"enabled": True, "token": "xoxb-x"}})
    )
    bridge = FileChannelsConfigBridge(path=bridge_path)
    block = bridge.read_bot_block("slack", "default")
    assert block["enabled"] is True
    # Empty bot_name is the strangler-fig legacy signal — it sees the
    # legacy dict entry too (matches set_bot_block's symmetrical fallback).
    assert bridge.read_bot_block("slack", "")["enabled"] is True
    # But a different name doesn't see the legacy entry.
    assert bridge.read_bot_block("slack", "hazbot") == {}


def test_read_bot_block_list_shape_matches_by_name(bridge_path):
    bridge_path.write_text(
        json.dumps({
            "slack": [
                {"name": "hazbot", "enabled": True},
                {"name": "support-bot", "enabled": False},
            ]
        })
    )
    bridge = FileChannelsConfigBridge(path=bridge_path)
    assert bridge.read_bot_block("slack", "hazbot")["enabled"] is True
    assert bridge.read_bot_block("slack", "support-bot")["enabled"] is False
    assert bridge.read_bot_block("slack", "missing") == {}


# ---------------------------------------------------------------------------
# list_bot_names
# ---------------------------------------------------------------------------


def test_list_bot_names_legacy_dict_returns_default(bridge_path):
    bridge_path.write_text(json.dumps({"slack": {"enabled": True}}))
    bridge = FileChannelsConfigBridge(path=bridge_path)
    assert bridge.list_bot_names("slack") == ["default"]


def test_list_bot_names_list_shape_returns_in_order(bridge_path):
    bridge_path.write_text(
        json.dumps({
            "slack": [
                {"name": "hazbot"},
                {"name": "support-bot"},
            ]
        })
    )
    bridge = FileChannelsConfigBridge(path=bridge_path)
    assert bridge.list_bot_names("slack") == ["hazbot", "support-bot"]


def test_list_bot_names_synthesizes_for_missing_name(bridge_path):
    """A list entry without 'name' falls back to bot<idx>."""

    bridge_path.write_text(json.dumps({"slack": [{"enabled": True}]}))
    bridge = FileChannelsConfigBridge(path=bridge_path)
    assert bridge.list_bot_names("slack") == ["bot0"]


def test_list_bot_names_missing_platform_returns_empty(bridge_path):
    bridge = FileChannelsConfigBridge(path=bridge_path)
    assert bridge.list_bot_names("slack") == []


# ---------------------------------------------------------------------------
# set_bot_block
# ---------------------------------------------------------------------------


def test_set_bot_block_creates_list_when_platform_missing(bridge_path):
    bridge = FileChannelsConfigBridge(path=bridge_path)
    bridge.set_bot_block("slack", "hazbot", {"enabled": True, "token": "x"})

    data = json.loads(bridge_path.read_text())
    assert data["slack"] == [{"name": "hazbot", "enabled": True, "token": "x"}]


def test_set_bot_block_normalizes_legacy_dict_to_list(bridge_path):
    """Pre-existing dict-shape entry is wrapped, then upsert proceeds."""

    bridge_path.write_text(
        json.dumps({"slack": {"enabled": True, "token": "xoxb-old"}})
    )
    bridge = FileChannelsConfigBridge(path=bridge_path)
    bridge.set_bot_block(
        "slack", "hazbot", {"enabled": True, "token": "$$secret(BTOK)"}
    )

    data = json.loads(bridge_path.read_text())
    assert isinstance(data["slack"], list)
    names = [e["name"] for e in data["slack"]]
    # The legacy entry survives as 'default'; new bot appended.
    assert names == ["default", "hazbot"]
    assert data["slack"][0]["token"] == "xoxb-old"
    assert data["slack"][1]["token"] == "$$secret(BTOK)"


def test_set_bot_block_upserts_existing_by_name(bridge_path):
    """Same bot_name = replace the entry, don't duplicate."""

    bridge_path.write_text(
        json.dumps({
            "slack": [
                {"name": "hazbot", "enabled": False, "token": "old"},
                {"name": "support-bot", "enabled": True, "token": "s"},
            ]
        })
    )
    bridge = FileChannelsConfigBridge(path=bridge_path)
    bridge.set_bot_block(
        "slack", "hazbot", {"enabled": True, "token": "new"}
    )

    data = json.loads(bridge_path.read_text())
    names = [e["name"] for e in data["slack"]]
    assert names == ["hazbot", "support-bot"], "no duplicate appended"
    assert data["slack"][0]["token"] == "new"
    # Other bots untouched.
    assert data["slack"][1]["token"] == "s"


def test_set_bot_block_empty_name_falls_back_to_legacy_writer(bridge_path):
    """Empty bot_name should not corrupt channels.json — it routes
    through the old single-bot writer (dict shape)."""

    bridge = FileChannelsConfigBridge(path=bridge_path)
    bridge.set_bot_block("slack", "", {"enabled": True, "token": "x"})

    data = json.loads(bridge_path.read_text())
    # update_block writes the dict-shape; loader migrates on next read.
    assert isinstance(data["slack"], dict)
    assert data["slack"]["enabled"] is True
