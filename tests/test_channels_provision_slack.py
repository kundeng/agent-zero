"""End-to-end tests for the Slack provisioner (spec 08 task 2.5).

All HTTP is mocked via :func:`urllib.request.urlopen` so the tests
run offline. Each test isolates ``usr/secrets.env`` and
``~/.hyperagent0/channels.json`` to a tmp path so writes don't leak.

The provisioner is the framework's first concrete platform — these
tests are also the proof that the framework hangs together
end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hyperagent0.channels.channels_config_bridge import FileChannelsConfigBridge
from hyperagent0.channels.provision import slack as slack_provisioner
from hyperagent0.channels.provision import slack_api
from hyperagent0.channels.provision.base import ProvisionContext
from hyperagent0.channels.secrets_bridge import AllowlistedSecretsBridge


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Tmp ``usr/secrets.env`` + tmp ``channels.json`` + reset singletons."""

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
    """Build a fake ``urllib.request.urlopen`` return value."""

    resp = MagicMock()
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: False
    resp.read = lambda: json.dumps(payload).encode("utf-8")
    return resp


def _make_ctx(channels_path: Path) -> ProvisionContext:
    return ProvisionContext(
        channel_type="slack",
        session_id="sess1",
        session={},
        secrets=AllowlistedSecretsBridge(
            slack_provisioner.SlackProvisioner.required_secrets
        ),
        channels_config=FileChannelsConfigBridge(path=channels_path),
        host_base_url="http://localhost:50080",
    )


# ---------------------------------------------------------------------------
# Step 1 — config_token / apps.manifest.create
# ---------------------------------------------------------------------------


def test_step_config_token_happy_path(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()

    # Two HTTP calls in sequence: apps.manifest.create, then auth.test
    # for team_id discovery.
    responses = iter(
        [
            _mocked_response(
                {
                    "ok": True,
                    "app_id": "A0SLACK",
                    "credentials": {
                        "client_id": "CID",
                        "client_secret": "CSEC",
                        "signing_secret": "SSEC",
                        "verification_token": "VTOK",
                    },
                    "oauth_authorize_url": "https://slack.com/oauth/v2/authorize?client_id=CID",
                }
            ),
            _mocked_response({"ok": True, "team_id": "T0WORK", "team": "acme"}),
        ]
    )
    with patch.object(
        slack_api.urllib.request,
        "urlopen",
        side_effect=lambda *a, **kw: next(responses),
    ):
        result = p.provision(
            "config_token",
            {
                "config_token": "xoxe.xoxp-test",
                "display_name": "hyperagent",
                "include_private_channels": True,
                "include_dms": True,
            },
            ctx,
        )

    assert result.error is None
    assert result.next_step == "install"
    assert result.url_override is not None
    assert "state=" in result.url_override
    assert result.state_token is not None
    # team= must be appended — Slack rejects the install with
    # invalid_team_for_non_distributed_app otherwise.
    assert "team=T0WORK" in result.url_override

    # Credentials persisted.
    secrets_text = (isolated_env["base"] / "usr" / "secrets.env").read_text()
    assert 'SLACK_APP_ID="A0SLACK"' in secrets_text
    assert 'SLACK_SIGNING_SECRET="SSEC"' in secrets_text
    assert 'SLACK_CLIENT_ID="CID"' in secrets_text
    assert 'SLACK_CLIENT_SECRET="CSEC"' in secrets_text

    # Config token NEVER persisted.
    assert "xoxe.xoxp-test" not in secrets_text

    # Session scratch holds the install URL + client id/secret.
    assert ctx.session["app_id"] == "A0SLACK"
    assert ctx.session["client_id"] == "CID"
    assert ctx.session["client_secret"] == "CSEC"


def test_step_config_token_missing_input(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()
    result = p.provision("config_token", {}, ctx)
    assert result.error is not None
    assert "config_token" in result.error


def test_step_config_token_invalid_manifest_surfaces_pointer(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()
    payload = {
        "ok": False,
        "error": "invalid_manifest",
        "errors": [
            {
                "pointer": "/features/bot_user/display_name",
                "message": "must be lowercase",
            },
        ],
    }
    with patch.object(slack_api.urllib.request, "urlopen", return_value=_mocked_response(payload)):
        result = p.provision(
            "config_token",
            {"config_token": "x", "display_name": "Bad Name"},
            ctx,
        )
    assert result.error is not None
    assert "/features/bot_user/display_name" in result.error
    assert result.error_pointer == "/features/bot_user/display_name"


def test_step_config_token_rotates_on_expired(isolated_env):
    """token_expired + refresh_token → rotate → retry succeeds."""

    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()

    expired_resp = _mocked_response({"ok": False, "error": "token_expired"})
    rotate_resp = _mocked_response(
        {"ok": True, "token": "xoxe.xoxp-new", "refresh_token": "xoxe-new-refresh"}
    )
    success_resp = _mocked_response(
        {
            "ok": True,
            "app_id": "A0",
            "credentials": {
                "client_id": "C", "client_secret": "CS",
                "signing_secret": "SS", "verification_token": "V",
            },
            "oauth_authorize_url": "https://slack.com/install",
        }
    )
    team_resp = _mocked_response({"ok": True, "team_id": "T0X"})

    call_count = {"n": 0}

    def _seq(*_args, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return expired_resp
        if call_count["n"] == 2:
            return rotate_resp
        if call_count["n"] == 3:
            return success_resp
        return team_resp

    with patch.object(slack_api.urllib.request, "urlopen", side_effect=_seq):
        result = p.provision(
            "config_token",
            {
                "config_token": "xoxe.xoxp-stale",
                "refresh_token": "xoxe-refresh",
                "display_name": "hyperagent",
            },
            ctx,
        )

    assert result.error is None
    assert result.next_step == "install"
    # 4 calls: manifest-create-failed, rotate, manifest-create-success, auth.test
    assert call_count["n"] == 4
    # New refresh token landed in session for any future rotation.
    assert ctx.session["refresh_token"] == "xoxe-new-refresh"


def test_step_config_token_expired_without_refresh_fails(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()
    with patch.object(
        slack_api.urllib.request, "urlopen",
        return_value=_mocked_response({"ok": False, "error": "token_expired"}),
    ):
        result = p.provision(
            "config_token",
            {"config_token": "x", "display_name": "hyperagent"},
            ctx,
        )
    assert result.error is not None
    assert "refresh token" in result.error.lower()


# ---------------------------------------------------------------------------
# Step 2 — OAuth callback
# ---------------------------------------------------------------------------


def _seed_for_oauth(ctx: ProvisionContext, state_token: str) -> None:
    """Stash what step 1 would have set up so we can test step 2 in isolation."""

    ctx.session["client_id"] = "CID"
    ctx.session["client_secret"] = "CSEC"
    ctx.session["redirect_url"] = "http://localhost:50080/channels_oauth_callback?channel_type=slack&session_id=sess1"
    ctx.session.setdefault("__state_tokens", {})[state_token] = 99999999999.0


def test_oauth_callback_happy_path(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    _seed_for_oauth(ctx, "STATE_GOOD")
    p = slack_provisioner.SlackProvisioner()

    payload = {
        "ok": True,
        "access_token": "xoxb-real-bot",
        "app_id": "A0SLACK",
        "team": {"id": "T0WORK", "name": "acme"},
    }
    with patch.object(slack_api.urllib.request, "urlopen", return_value=_mocked_response(payload)):
        result = p.oauth_callback(
            {"code": "oauth-code", "state": "STATE_GOOD"}, ctx
        )

    assert result.error is None
    assert result.next_step == "app_token"
    secrets_text = (isolated_env["base"] / "usr" / "secrets.env").read_text()
    assert 'SLACK_BOT_TOKEN="xoxb-real-bot"' in secrets_text
    assert 'SLACK_TEAM_ID="T0WORK"' in secrets_text


def test_oauth_callback_rejects_missing_code(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    _seed_for_oauth(ctx, "STATE_GOOD")
    p = slack_provisioner.SlackProvisioner()
    result = p.oauth_callback({"state": "STATE_GOOD"}, ctx)
    assert result.error is not None
    assert "code" in result.error.lower()


def test_oauth_callback_rejects_bad_state(isolated_env):
    """Without a valid state token, the callback must refuse."""

    ctx = _make_ctx(isolated_env["channels_path"])
    _seed_for_oauth(ctx, "STATE_GOOD")
    p = slack_provisioner.SlackProvisioner()
    result = p.oauth_callback(
        {"code": "oauth-code", "state": "STATE_FORGED"}, ctx
    )
    assert result.error is not None
    assert "state" in result.error.lower()


def test_oauth_callback_state_single_use(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    _seed_for_oauth(ctx, "STATE_GOOD")
    p = slack_provisioner.SlackProvisioner()
    payload = {
        "ok": True,
        "access_token": "xoxb-1",
        "app_id": "A",
        "team": {"id": "T"},
    }
    with patch.object(slack_api.urllib.request, "urlopen", return_value=_mocked_response(payload)):
        # First use succeeds.
        r1 = p.oauth_callback({"code": "c", "state": "STATE_GOOD"}, ctx)
        assert r1.error is None
        # Second use with same state must fail (token consumed).
        r2 = p.oauth_callback({"code": "c", "state": "STATE_GOOD"}, ctx)
        assert r2.error is not None


def test_oauth_callback_missing_session_credentials(isolated_env):
    """If step 1's session state is gone, the callback must refuse."""

    ctx = _make_ctx(isolated_env["channels_path"])
    # No _seed_for_oauth — session is empty.
    ctx.session.setdefault("__state_tokens", {})["S"] = 99999999999.0
    p = slack_provisioner.SlackProvisioner()
    result = p.oauth_callback({"code": "c", "state": "S"}, ctx)
    assert result.error is not None


# ---------------------------------------------------------------------------
# Step 2-fallback — paste bot token
# ---------------------------------------------------------------------------


def test_paste_bot_token_happy_path(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()
    result = p.provision(
        "install_paste_fallback",
        {"bot_token": "xoxb-pasted"},
        ctx,
    )
    assert result.error is None
    assert result.next_step == "app_token"
    secrets_text = (isolated_env["base"] / "usr" / "secrets.env").read_text()
    assert 'SLACK_BOT_TOKEN="xoxb-pasted"' in secrets_text


def test_paste_bot_token_rejects_wrong_prefix(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()
    result = p.provision(
        "install_paste_fallback",
        {"bot_token": "xoxp-this-is-a-user-token"},
        ctx,
    )
    assert result.error is not None
    assert "xoxb-" in result.error


# ---------------------------------------------------------------------------
# Step 3 — app_token paste
# ---------------------------------------------------------------------------


def test_app_token_paste_happy_path(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    # Pre-write bot token (would have come from step 2).
    ctx.secrets.write({"SLACK_BOT_TOKEN": "xoxb-pre"})

    p = slack_provisioner.SlackProvisioner()
    result = p.provision(
        "app_token", {"app_token": "xapp-1-real"}, ctx
    )
    assert result.error is None
    assert result.next_step == "summary"

    secrets_text = (isolated_env["base"] / "usr" / "secrets.env").read_text()
    assert 'SLACK_APP_TOKEN="xapp-1-real"' in secrets_text

    # channels.json now has the Slack block with placeholders.
    chjson = json.loads(isolated_env["channels_path"].read_text())
    assert chjson["slack"]["enabled"] is True
    assert chjson["slack"]["token"] == "$$secret(SLACK_BOT_TOKEN)"
    assert chjson["slack"]["app_token"] == "$$secret(SLACK_APP_TOKEN)"
    # Raw tokens NEVER in channels.json.
    assert "xoxb" not in isolated_env["channels_path"].read_text()
    assert "xapp" not in isolated_env["channels_path"].read_text()


def test_app_token_paste_rejects_wrong_prefix(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()
    result = p.provision(
        "app_token", {"app_token": "xoxb-not-app-level"}, ctx
    )
    assert result.error is not None
    assert "xapp-" in result.error


# ---------------------------------------------------------------------------
# Step 4 — summary is terminal
# ---------------------------------------------------------------------------


def test_summary_is_terminal(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()
    result = p.provision("summary", {}, ctx)
    assert result.terminal is True
    assert result.error is None


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


def test_test_connection_with_mocked_auth_test(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    ctx.secrets.write({"SLACK_BOT_TOKEN": "xoxb-x"})
    p = slack_provisioner.SlackProvisioner()
    with patch.object(
        slack_api.urllib.request, "urlopen",
        return_value=_mocked_response({
            "ok": True, "user": "hyperagent", "team": "acme",
            "team_id": "T0", "user_id": "UBOT",
        }),
    ):
        msg = p.test_connection(ctx)
    assert "@hyperagent" in msg
    assert "acme" in msg


def test_test_connection_without_token_raises(isolated_env):
    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()
    with pytest.raises(RuntimeError, match="not configured"):
        p.test_connection(ctx)


# ---------------------------------------------------------------------------
# Wizard descriptor
# ---------------------------------------------------------------------------


def test_wizard_steps_has_five_steps():
    p = slack_provisioner.SlackProvisioner()
    steps = p.wizard_steps()
    ids = [s.id for s in steps]
    assert ids == [
        "config_token",
        "install",
        "install_paste_fallback",
        "app_token",
        "summary",
    ]


def test_wizard_step_kinds():
    p = slack_provisioner.SlackProvisioner()
    steps = {s.id: s for s in p.wizard_steps()}
    assert steps["config_token"].kind == "input"
    assert steps["install"].kind == "link_with_callback"
    assert steps["install_paste_fallback"].kind == "link_with_paste"
    assert steps["app_token"].kind == "link_with_paste"
    assert steps["summary"].kind == "summary"


def test_required_secrets_complete():
    """The declared allow-list must cover every secret the provisioner writes."""

    expected = {
        "SLACK_APP_ID",
        "SLACK_TEAM_ID",
        "SLACK_SIGNING_SECRET",
        "SLACK_CLIENT_ID",
        "SLACK_CLIENT_SECRET",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
    }
    assert set(slack_provisioner.SlackProvisioner.required_secrets) == expected
