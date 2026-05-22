"""Tests for the Discord provisioner (spec 08 task 2.7)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hyperagent0.channels.channels_config_bridge import FileChannelsConfigBridge
from hyperagent0.channels.provision import discord as dc_provisioner
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
        channel_type="discord",
        session_id="d1",
        session={},
        secrets=AllowlistedSecretsBridge(
            dc_provisioner.DiscordProvisioner.required_secrets
        ),
        channels_config=FileChannelsConfigBridge(path=env["channels_path"]),
    )


def test_credentials_happy_path(isolated_env):
    p = dc_provisioner.DiscordProvisioner()
    ctx = _ctx(isolated_env)
    with patch.object(
        dc_provisioner.urllib.request, "urlopen",
        return_value=_mocked_response({
            "id": "9999999", "username": "hyperagent_bot",
            "discriminator": "0001",
        }),
    ):
        r = p.provision(
            "credentials",
            {"bot_token": "tok-abc", "application_id": "123456789012345678"},
            ctx,
        )
    assert r.error is None
    assert r.next_step == "invite"
    assert r.url_override is not None
    assert "client_id=123456789012345678" in r.url_override
    assert "scope=bot" in r.url_override

    env_text = (isolated_env["base"] / "usr" / "secrets.env").read_text()
    assert 'DISCORD_BOT_TOKEN="tok-abc"' in env_text
    assert 'DISCORD_APPLICATION_ID="123456789012345678"' in env_text


def test_credentials_missing_token(isolated_env):
    p = dc_provisioner.DiscordProvisioner()
    r = p.provision(
        "credentials",
        {"application_id": "123456"},
        _ctx(isolated_env),
    )
    assert r.error is not None
    assert r.error_pointer == "/bot_token"


def test_credentials_missing_application_id(isolated_env):
    p = dc_provisioner.DiscordProvisioner()
    r = p.provision(
        "credentials",
        {"bot_token": "tok"},
        _ctx(isolated_env),
    )
    assert r.error is not None
    assert r.error_pointer == "/application_id"


def test_credentials_non_numeric_application_id(isolated_env):
    p = dc_provisioner.DiscordProvisioner()
    r = p.provision(
        "credentials",
        {"bot_token": "tok", "application_id": "not-a-snowflake"},
        _ctx(isolated_env),
    )
    assert r.error is not None
    assert "numeric" in r.error.lower()


def test_credentials_users_me_failure_surfaces(isolated_env):
    """Bad token → /users/@me returns 401 → user-facing error."""

    import urllib.error

    p = dc_provisioner.DiscordProvisioner()
    ctx = _ctx(isolated_env)
    fake_err = urllib.error.HTTPError(
        url="https://discord.com/api/v10/users/@me",
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    with patch.object(dc_provisioner.urllib.request, "urlopen", side_effect=fake_err):
        r = p.provision(
            "credentials",
            {"bot_token": "bad", "application_id": "123"},
            ctx,
        )
    assert r.error is not None
    assert "401" in r.error


def test_invite_step_writes_channels_block(isolated_env):
    p = dc_provisioner.DiscordProvisioner()
    ctx = _ctx(isolated_env)
    # Simulate the post-credentials state.
    ctx.session["application_id"] = "9999"
    ctx.secrets.write({"DISCORD_BOT_TOKEN": "t", "DISCORD_APPLICATION_ID": "9999"})

    r = p.provision("invite", {"confirm": "done"}, ctx)
    assert r.error is None
    assert r.next_step == "summary"

    chjson = json.loads(isolated_env["channels_path"].read_text())
    assert chjson["discord"]["enabled"] is True
    assert chjson["discord"]["token"] == "$$secret(DISCORD_BOT_TOKEN)"
    assert chjson["discord"]["application_id"] == "9999"


def test_invite_step_requires_confirm(isolated_env):
    p = dc_provisioner.DiscordProvisioner()
    r = p.provision("invite", {}, _ctx(isolated_env))
    assert r.error is not None
    assert r.error_pointer == "/confirm"


def test_summary_terminal(isolated_env):
    p = dc_provisioner.DiscordProvisioner()
    r = p.provision("summary", {}, _ctx(isolated_env))
    assert r.terminal is True


def test_oauth_callback_unsupported(isolated_env):
    p = dc_provisioner.DiscordProvisioner()
    r = p.oauth_callback({}, _ctx(isolated_env))
    assert r.error is not None


def test_test_connection_with_token(isolated_env):
    p = dc_provisioner.DiscordProvisioner()
    ctx = _ctx(isolated_env)
    ctx.secrets.write({"DISCORD_BOT_TOKEN": "tok", "DISCORD_APPLICATION_ID": "1"})
    with patch.object(
        dc_provisioner.urllib.request, "urlopen",
        return_value=_mocked_response({"username": "bot", "discriminator": "9999"}),
    ):
        msg = p.test_connection(ctx)
    assert "bot#9999" in msg


def test_build_invite_url_contains_required_params():
    url = dc_provisioner.DiscordProvisioner._build_invite_url("1234567890")
    assert "client_id=1234567890" in url
    assert "scope=bot" in url
    assert "permissions=" in url


def test_wizard_steps_shape():
    p = dc_provisioner.DiscordProvisioner()
    ids = [s.id for s in p.wizard_steps()]
    assert ids == ["credentials", "invite", "summary"]
