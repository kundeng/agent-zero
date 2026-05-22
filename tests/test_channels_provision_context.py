"""Tests for ProvisionContext, sessions cache, and the two bridges (spec 08 task 2.4).

The session cache TTL + cross-platform isolation, the
:class:`AllowlistedSecretsBridge` allow-list rejection, and the
:class:`FileChannelsConfigBridge` atomic-rename writes are all
load-bearing for the framework's safety claims. These tests pin
those behaviors.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from hyperagent0.channels.channels_config_bridge import FileChannelsConfigBridge
from hyperagent0.channels.provision.base import ProvisionContext
from hyperagent0.channels.provision.sessions import SessionCache
from hyperagent0.channels.secrets_bridge import AllowlistedSecretsBridge


# ---------------------------------------------------------------------------
# SessionCache
# ---------------------------------------------------------------------------


def test_session_cache_start_and_get():
    c = SessionCache(ttl_s=60)
    s = c.start("slack")
    assert c.get(s.session_id, "slack") is s
    assert c.size() == 1


def test_session_cache_get_rejects_channel_type_mismatch():
    """A Slack session must not be retrievable via Telegram channel_type."""

    c = SessionCache(ttl_s=60)
    s = c.start("slack")
    assert c.get(s.session_id, "telegram") is None


def test_session_cache_ttl_evicts():
    c = SessionCache(ttl_s=1)
    s = c.start("slack")
    time.sleep(1.2)
    assert c.get(s.session_id, "slack") is None


def test_session_cache_get_or_start_returns_existing():
    c = SessionCache(ttl_s=60)
    s1 = c.start("slack")
    s2 = c.get_or_start("slack", s1.session_id)
    assert s2 is s1


def test_session_cache_get_or_start_creates_new_when_missing():
    c = SessionCache(ttl_s=60)
    s = c.get_or_start("slack", "no-such-id")
    assert s.channel_type == "slack"
    assert s.session_id != "no-such-id"


def test_session_cache_get_or_start_creates_new_on_type_mismatch():
    """Sneaking a Slack session id past a Telegram start mints a fresh one."""

    c = SessionCache(ttl_s=60)
    s_slack = c.start("slack")
    s_telegram = c.get_or_start("telegram", s_slack.session_id)
    assert s_telegram.channel_type == "telegram"
    assert s_telegram.session_id != s_slack.session_id


def test_session_cache_end_drops():
    c = SessionCache(ttl_s=60)
    s = c.start("slack")
    c.end(s.session_id)
    assert c.get(s.session_id, "slack") is None


# ---------------------------------------------------------------------------
# ProvisionContext state tokens (CSRF)
# ---------------------------------------------------------------------------


def _make_ctx() -> ProvisionContext:
    return ProvisionContext(
        channel_type="slack",
        session_id="sess1",
        session={},
        secrets=AllowlistedSecretsBridge(["DUMMY"]),
        channels_config=FileChannelsConfigBridge(path=Path("/tmp/__unused.json")),
    )


def test_state_token_round_trip():
    ctx = _make_ctx()
    tok = ctx.new_state_token()
    assert ctx.consume_state_token(tok) is True


def test_state_token_consumed_only_once():
    ctx = _make_ctx()
    tok = ctx.new_state_token()
    assert ctx.consume_state_token(tok) is True
    assert ctx.consume_state_token(tok) is False  # already consumed


def test_state_token_unknown_rejected():
    ctx = _make_ctx()
    assert ctx.consume_state_token("bogus") is False


def test_state_token_expires():
    ctx = _make_ctx()
    tok = ctx.new_state_token()
    # Force the issue time into the past.
    ctx.session["__state_tokens"][tok] = time.time() - 9999
    assert ctx.consume_state_token(tok, max_age_s=60) is False


# ---------------------------------------------------------------------------
# AllowlistedSecretsBridge
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_secrets_env(tmp_path, monkeypatch):
    """Redirect upstream's base dir + reset SecretsManager singleton.

    Each test gets its own temp tree so writes never touch the real
    ``usr/secrets.env``.
    """

    base = tmp_path / "repo"
    (base / "usr").mkdir(parents=True)
    (base / "usr" / "secrets.env").write_text("")

    from python.helpers import files as files_mod

    monkeypatch.setattr(files_mod, "get_base_dir", lambda: str(base))

    from python.helpers.secrets import SecretsManager

    SecretsManager._instances = {}
    return base


def test_secrets_bridge_writes_then_reads(isolated_secrets_env):
    bridge = AllowlistedSecretsBridge(["A_KEY", "B_KEY"])
    bridge.write({"A_KEY": "a-val", "B_KEY": "b-val"})

    assert bridge.read("A_KEY") == "a-val"
    assert bridge.read("B_KEY") == "b-val"

    env_text = (isolated_secrets_env / "usr" / "secrets.env").read_text()
    assert 'A_KEY="a-val"' in env_text
    assert 'B_KEY="b-val"' in env_text


def test_secrets_bridge_updates_existing_in_place(isolated_secrets_env):
    bridge = AllowlistedSecretsBridge(["TOKEN"])
    bridge.write({"TOKEN": "first"})
    bridge.write({"TOKEN": "second"})
    env_text = (isolated_secrets_env / "usr" / "secrets.env").read_text()
    # Only one TOKEN line — update-in-place, not append.
    assert env_text.count('TOKEN="') == 1
    assert 'TOKEN="second"' in env_text
    assert 'TOKEN="first"' not in env_text


def test_secrets_bridge_preserves_unrelated_keys(isolated_secrets_env):
    """Crucial: a provisioner writing its keys must not delete others."""

    (isolated_secrets_env / "usr" / "secrets.env").write_text(
        'OTHER_KEY="keep me"\n'
    )
    from python.helpers.secrets import SecretsManager
    SecretsManager._instances = {}

    bridge = AllowlistedSecretsBridge(["MINE"])
    bridge.write({"MINE": "mine-val"})

    env_text = (isolated_secrets_env / "usr" / "secrets.env").read_text()
    assert 'OTHER_KEY="keep me"' in env_text
    assert 'MINE="mine-val"' in env_text


def test_secrets_bridge_rejects_undeclared_keys(isolated_secrets_env):
    bridge = AllowlistedSecretsBridge(["ALLOWED"])
    with pytest.raises(ValueError, match="STRAY_KEY"):
        bridge.write({"ALLOWED": "ok", "STRAY_KEY": "nope"})


def test_secrets_bridge_read_returns_none_for_missing(isolated_secrets_env):
    bridge = AllowlistedSecretsBridge(["MAY_BE_THERE"])
    assert bridge.read("MAY_BE_THERE") is None


def test_secrets_bridge_escapes_quotes(isolated_secrets_env):
    bridge = AllowlistedSecretsBridge(["WEIRD"])
    bridge.write({"WEIRD": 'has "quotes" and \\backslash'})
    env_text = (isolated_secrets_env / "usr" / "secrets.env").read_text()
    assert "\\\"quotes\\\"" in env_text  # quotes escaped
    assert "\\\\backslash" in env_text


# ---------------------------------------------------------------------------
# FileChannelsConfigBridge
# ---------------------------------------------------------------------------


def test_channels_bridge_read_returns_empty_for_missing_file(tmp_path):
    bridge = FileChannelsConfigBridge(path=tmp_path / "channels.json")
    assert bridge.read_block("slack") == {}


def test_channels_bridge_write_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "subdir" / "channels.json"
    bridge = FileChannelsConfigBridge(path=path)
    bridge.update_block("slack", {"enabled": True})
    assert path.exists()


def test_channels_bridge_atomic_write_leaves_no_tmp_files(tmp_path):
    path = tmp_path / "channels.json"
    bridge = FileChannelsConfigBridge(path=path)
    bridge.update_block("slack", {"enabled": False})
    bridge.update_block("telegram", {"enabled": True})
    bridge.update_block("slack", {"enabled": True})
    # No leftover .tmp files in the directory.
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_channels_bridge_preserves_unrelated_channels(tmp_path):
    path = tmp_path / "channels.json"
    bridge = FileChannelsConfigBridge(path=path)
    bridge.update_block("slack", {"enabled": True, "token": "$$secret(X)"})
    bridge.update_block("telegram", {"enabled": False})
    data = json.loads(path.read_text())
    assert data["slack"]["enabled"] is True
    assert data["telegram"]["enabled"] is False


def test_channels_bridge_project_binding_default(tmp_path):
    path = tmp_path / "channels.json"
    bridge = FileChannelsConfigBridge(path=path)
    bridge.update_block("slack", {"enabled": True})
    bridge.update_project_binding("slack", chat_id=None, project_name="personal")
    data = json.loads(path.read_text())
    assert data["slack"]["project_binding"]["default"] == "personal"
    assert data["slack"]["enabled"] is True  # preserved


def test_channels_bridge_project_binding_per_chat(tmp_path):
    path = tmp_path / "channels.json"
    bridge = FileChannelsConfigBridge(path=path)
    bridge.update_project_binding("slack", chat_id="C123", project_name="work")
    data = json.loads(path.read_text())
    assert data["slack"]["project_binding"]["C123"] == "work"


def test_channels_bridge_project_binding_clear_with_none(tmp_path):
    path = tmp_path / "channels.json"
    bridge = FileChannelsConfigBridge(path=path)
    bridge.update_project_binding("slack", chat_id="C123", project_name="work")
    bridge.update_project_binding("slack", chat_id="C123", project_name=None)
    data = json.loads(path.read_text())
    assert "C123" not in data["slack"]["project_binding"]
