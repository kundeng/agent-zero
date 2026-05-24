"""Tests for spec 09's multi-bot channel config schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperagent0.channels.config import BotConfig, load_bot_configs


def _write_channels(tmp_path: Path, payload: dict) -> Path:
    cj = tmp_path / "channels.json"
    cj.write_text(json.dumps(payload))
    return cj


def test_backward_compat_dict_shape_wraps_as_single_bot(tmp_path):
    """Old spec-04/08 schema (dict per platform) loads as one-bot list."""

    cj = _write_channels(tmp_path, {
        "slack": {"enabled": True, "token": "xoxb-x", "app_token": "xapp-y"},
    })
    bots = load_bot_configs(channels_json=cj, settings_json=Path("/nonexistent"))

    assert len(bots["slack"]) == 1
    assert bots["slack"][0].bot_name == "default"
    assert bots["slack"][0].token == "xoxb-x"
    assert bots["slack"][0].app_token == "xapp-y"
    assert bots["slack"][0].enabled is True


def test_new_list_shape_multiple_bots(tmp_path):
    cj = _write_channels(tmp_path, {
        "slack": [
            {"name": "hazbot", "enabled": True, "token": "xoxb-1",
             "app_token": "xapp-1", "default_project": "engineering"},
            {"name": "support-bot", "enabled": True, "token": "xoxb-2",
             "app_token": "xapp-2", "default_project": "customer-ops"},
        ],
    })
    bots = load_bot_configs(channels_json=cj, settings_json=Path("/nonexistent"))

    assert [b.bot_name for b in bots["slack"]] == ["hazbot", "support-bot"]
    assert bots["slack"][0].default_project == "engineering"
    assert bots["slack"][1].default_project == "customer-ops"


def test_list_shape_synthesizes_name_when_missing(tmp_path):
    """If a list entry doesn't specify name, the loader gives it bot<idx>."""

    cj = _write_channels(tmp_path, {
        "telegram": [{"enabled": True, "token": "123:abc"}],
    })
    bots = load_bot_configs(channels_json=cj, settings_json=Path("/nonexistent"))
    assert bots["telegram"][0].bot_name == "bot0"


def test_project_for_chat_resolves_override():
    b = BotConfig(
        channel_type="slack",
        bot_name="hazbot",
        default_project="engineering",
        project_overrides={"C12345": "support"},
    )
    assert b.project_for_chat("C12345") == "support"
    assert b.project_for_chat("C99999") == "engineering"


def test_project_for_chat_falls_back_to_default_project():
    b = BotConfig(
        channel_type="slack",
        bot_name="hazbot",
        default_project="",  # explicit empty
    )
    # _default kicks in via resolve_project_name
    assert b.project_for_chat("C12345") == "_default"


def test_settings_json_overridden_by_channels_json(tmp_path):
    """When both files have a platform entry, channels.json wins (replaces)."""

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "channels": {"slack": {"enabled": True, "token": "xoxb-old"}}
    }))
    cj = _write_channels(tmp_path, {
        "slack": [{"name": "new-bot", "enabled": True, "token": "xoxb-new"}],
    })
    bots = load_bot_configs(channels_json=cj, settings_json=settings)
    assert len(bots["slack"]) == 1
    assert bots["slack"][0].bot_name == "new-bot"
    assert bots["slack"][0].token == "xoxb-new"


def test_missing_file_returns_empty(tmp_path):
    bots = load_bot_configs(
        channels_json=tmp_path / "absent.json",
        settings_json=tmp_path / "also-absent.json",
    )
    assert bots == {}


def test_project_overrides_accepts_old_project_binding_key(tmp_path):
    """Spec-08 used `project_binding`; spec-09 renames to `project_overrides`.

    Loader accepts both for backward-compat.
    """

    cj = _write_channels(tmp_path, {
        "slack": {
            "enabled": True, "token": "xoxb",
            "project_binding": {"C111": "alpha"},
        },
    })
    bots = load_bot_configs(channels_json=cj, settings_json=Path("/nonexistent"))
    assert bots["slack"][0].project_overrides == {"C111": "alpha"}
