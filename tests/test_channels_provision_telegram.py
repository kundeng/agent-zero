"""Tests for the Telegram provisioner (spec 08 task 2.6).

Mocks Telegram's ``getMe`` + ``setMyCommands`` via monkeypatched
``urlopen`` so no real bot is hit.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hyperagent0.channels.channels_config_bridge import FileChannelsConfigBridge
from hyperagent0.channels.provision import telegram as tg_provisioner
from hyperagent0.channels.provision.base import ProvisionContext
from hyperagent0.channels.secrets_bridge import AllowlistedSecretsBridge


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    base = tmp_path / "repo"
    (base / "usr").mkdir(parents=True)
    (base / "usr" / "secrets.env").write_text("")
    from python.helpers import files as files_mod

    monkeypatch.setattr(files_mod, "get_base_dir", lambda: str(base))
    from python.helpers.secrets import SecretsManager

    SecretsManager._instances = {}
    channels_path = tmp_path / "channels.json"
    import hyperagent0.channels.channels_config_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "_channels_path", lambda: channels_path)
    return {"base": base, "channels_path": channels_path}


def _mocked_response(payload):
    resp = MagicMock()
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: False
    resp.read = lambda: json.dumps(payload).encode("utf-8")
    return resp


def _ctx(env) -> ProvisionContext:
    return ProvisionContext(
        channel_type="telegram",
        session_id="t1",
        session={},
        secrets=AllowlistedSecretsBridge(
            tg_provisioner.TelegramProvisioner.required_secrets
        ),
        channels_config=FileChannelsConfigBridge(path=env["channels_path"]),
    )


def test_happy_path_writes_secret_and_block(isolated_env):
    p = tg_provisioner.TelegramProvisioner()
    ctx = _ctx(isolated_env)
    with patch.object(
        tg_provisioner.urllib.request, "urlopen",
        return_value=_mocked_response({
            "ok": True, "result": {"id": 123, "username": "hyperagent_bot"},
        }),
    ):
        r = p.provision(
            "bot_token",
            {"bot_token": "12345:ABC-DEF"},
            ctx,
        )
    assert r.error is None
    assert r.next_step == "summary"

    env_text = (isolated_env["base"] / "usr" / "secrets.env").read_text()
    assert 'TELEGRAM_BOT_TOKEN="12345:ABC-DEF"' in env_text

    chjson = json.loads(isolated_env["channels_path"].read_text())
    assert chjson["telegram"]["enabled"] is True
    assert chjson["telegram"]["token"] == "$$secret(TELEGRAM_BOT_TOKEN)"


def test_missing_bot_token_errors(isolated_env):
    p = tg_provisioner.TelegramProvisioner()
    r = p.provision("bot_token", {}, _ctx(isolated_env))
    assert r.error is not None


def test_get_me_failure_surfaces_as_error(isolated_env):
    p = tg_provisioner.TelegramProvisioner()
    ctx = _ctx(isolated_env)
    with patch.object(
        tg_provisioner.urllib.request, "urlopen",
        return_value=_mocked_response({
            "ok": False, "description": "Unauthorized",
        }),
    ):
        r = p.provision("bot_token", {"bot_token": "bad"}, ctx)
    assert r.error is not None
    assert "Unauthorized" in r.error


def test_allowed_users_csv_lands_in_block(isolated_env):
    p = tg_provisioner.TelegramProvisioner()
    ctx = _ctx(isolated_env)
    with patch.object(
        tg_provisioner.urllib.request, "urlopen",
        return_value=_mocked_response({
            "ok": True, "result": {"username": "b"},
        }),
    ):
        p.provision(
            "bot_token",
            {"bot_token": "t", "allowed_users": "111, 222, 333"},
            ctx,
        )
    chjson = json.loads(isolated_env["channels_path"].read_text())
    assert chjson["telegram"]["allowed_users"] == ["111", "222", "333"]


def test_commands_block_parses(isolated_env):
    p = tg_provisioner.TelegramProvisioner()
    ctx = _ctx(isolated_env)
    # The getMe call AND the setMyCommands call both succeed.
    responses = iter([
        _mocked_response({"ok": True, "result": {"username": "b"}}),
        _mocked_response({"ok": True, "result": True}),
    ])
    with patch.object(
        tg_provisioner.urllib.request, "urlopen",
        side_effect=lambda *a, **kw: next(responses),
    ):
        r = p.provision(
            "bot_token",
            {
                "bot_token": "t",
                "commands": "start: Begin chatting\nhelp: Show help",
            },
            ctx,
        )
    assert r.error is None


def test_commands_setmycommands_failure_is_nonfatal(isolated_env):
    """If setMyCommands fails after getMe succeeded, provisioning still completes."""

    p = tg_provisioner.TelegramProvisioner()
    ctx = _ctx(isolated_env)
    responses = iter([
        _mocked_response({"ok": True, "result": {"username": "b"}}),
        _mocked_response({"ok": False, "description": "set commands failed"}),
    ])
    with patch.object(
        tg_provisioner.urllib.request, "urlopen",
        side_effect=lambda *a, **kw: next(responses),
    ):
        r = p.provision(
            "bot_token",
            {"bot_token": "t", "commands": "start: hi"},
            ctx,
        )
    # getMe succeeded → bot token saved → next_step advances.
    assert r.error is None
    assert r.next_step == "summary"


def test_summary_is_terminal(isolated_env):
    p = tg_provisioner.TelegramProvisioner()
    r = p.provision("summary", {}, _ctx(isolated_env))
    assert r.terminal is True


def test_oauth_callback_returns_unsupported(isolated_env):
    p = tg_provisioner.TelegramProvisioner()
    r = p.oauth_callback({}, _ctx(isolated_env))
    assert r.error is not None


def test_test_connection_uses_stored_token(isolated_env):
    p = tg_provisioner.TelegramProvisioner()
    ctx = _ctx(isolated_env)
    ctx.secrets.write({"TELEGRAM_BOT_TOKEN": "stored-token"})
    with patch.object(
        tg_provisioner.urllib.request, "urlopen",
        return_value=_mocked_response({
            "ok": True, "result": {"username": "stored_bot"},
        }),
    ):
        msg = p.test_connection(ctx)
    assert "@stored_bot" in msg


def test_wizard_steps_has_two_steps():
    p = tg_provisioner.TelegramProvisioner()
    ids = [s.id for s in p.wizard_steps()]
    assert ids == ["bot_token", "summary"]


def test_parse_commands_skips_malformed_lines():
    raw = "good: ok\nno-colon-line\n\n: empty-cmd\n/leading-slash: stripped"
    parsed = tg_provisioner._parse_commands(raw)
    assert {"command": "good", "description": "ok"} in parsed
    assert {"command": "leading-slash", "description": "stripped"} in parsed
    # No-colon and empty-cmd lines must be dropped.
    assert len(parsed) == 2
