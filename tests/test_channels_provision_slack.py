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


def test_wizard_steps_d10_layout():
    """Spec 08 D10: the wizard takes 4 steps (manifest_config →
    paste_bot_token → paste_app_token → summary). The legacy
    config_token/install/install_paste_fallback path is still callable
    from provision() but not surfaced by wizard_steps()."""

    p = slack_provisioner.SlackProvisioner()
    steps = p.wizard_steps()
    ids = [s.id for s in steps]
    assert ids == [
        "manifest_config",
        "paste_bot_token",
        "paste_app_token",
        "summary",
    ]


def test_wizard_step_kinds():
    p = slack_provisioner.SlackProvisioner()
    steps = {s.id: s for s in p.wizard_steps()}
    assert steps["manifest_config"].kind == "input"
    assert steps["paste_bot_token"].kind == "link_with_paste"
    assert steps["paste_app_token"].kind == "link_with_paste"
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


# ---------------------------------------------------------------------------
# Spec 09 D5 — named-bot provisioning writes per-bot keys + list-shape JSON
# ---------------------------------------------------------------------------


def _make_named_ctx(channels_path: Path, bot_name: str) -> ProvisionContext:
    """Provision context targeting a non-default bot.

    Mirrors what dispatch.make_context produces when the wizard's first
    step supplies ``bot_name``: the secrets bridge's allow-list switches
    to per-bot suffixed keys, and ctx.bot_name is populated so the
    provisioner can compose the right placeholders.
    """

    return ProvisionContext(
        channel_type="slack",
        session_id="sess-named",
        session={"bot_name": bot_name},
        secrets=AllowlistedSecretsBridge(
            slack_provisioner.SlackProvisioner.required_secrets,
            bot_name=bot_name,
        ),
        channels_config=FileChannelsConfigBridge(path=channels_path),
        host_base_url="http://localhost:50080",
        bot_name=bot_name,
    )


def test_named_bot_paste_token_writes_suffixed_secret(isolated_env):
    """Bot named 'hazbot' → SLACK_BOT_TOKEN_HAZBOT lands in secrets.env."""

    ctx = _make_named_ctx(isolated_env["channels_path"], "hazbot")
    p = slack_provisioner.SlackProvisioner()
    result = p.provision(
        "install_paste_fallback",
        {"bot_token": "xoxb-named-bot"},
        ctx,
    )
    assert result.error is None
    secrets_text = (isolated_env["base"] / "usr" / "secrets.env").read_text()
    assert 'SLACK_BOT_TOKEN_HAZBOT="xoxb-named-bot"' in secrets_text
    # Bare key must NOT be written for a named bot.
    assert 'SLACK_BOT_TOKEN="' not in secrets_text


def test_named_bot_app_token_appends_to_channels_list_shape(isolated_env):
    """Final write goes through set_bot_block — list-shape, by-name upsert."""

    ctx = _make_named_ctx(isolated_env["channels_path"], "hazbot")
    # Pre-write the bot token under per-bot suffix (would come from step 2).
    ctx.secrets.write({"SLACK_BOT_TOKEN_HAZBOT": "xoxb-pre"})

    p = slack_provisioner.SlackProvisioner()
    result = p.provision("app_token", {"app_token": "xapp-named"}, ctx)
    assert result.error is None

    chjson = json.loads(isolated_env["channels_path"].read_text())
    # List-shape — one entry, our bot.
    assert isinstance(chjson["slack"], list)
    assert len(chjson["slack"]) == 1
    entry = chjson["slack"][0]
    assert entry["name"] == "hazbot"
    assert entry["enabled"] is True
    assert entry["token"] == "$$secret(SLACK_BOT_TOKEN_HAZBOT)"
    assert entry["app_token"] == "$$secret(SLACK_APP_TOKEN_HAZBOT)"
    # Raw tokens still never in channels.json.
    assert "xoxb" not in isolated_env["channels_path"].read_text()
    assert "xapp" not in isolated_env["channels_path"].read_text()


def test_two_named_bots_upsert_into_list(isolated_env):
    """Provisioning a second bot leaves the first intact and appends."""

    p = slack_provisioner.SlackProvisioner()

    # First bot.
    ctx1 = _make_named_ctx(isolated_env["channels_path"], "hazbot")
    ctx1.secrets.write({"SLACK_BOT_TOKEN_HAZBOT": "xoxb-1"})
    p.provision("app_token", {"app_token": "xapp-1"}, ctx1)

    # Second bot — fresh context, different name.
    ctx2 = _make_named_ctx(isolated_env["channels_path"], "support-bot")
    ctx2.secrets.write({"SLACK_BOT_TOKEN_SUPPORT_BOT": "xoxb-2"})
    p.provision("app_token", {"app_token": "xapp-2"}, ctx2)

    chjson = json.loads(isolated_env["channels_path"].read_text())
    names = [e["name"] for e in chjson["slack"]]
    assert names == ["hazbot", "support-bot"]
    # Each bot's placeholders are independent.
    assert chjson["slack"][0]["token"] == "$$secret(SLACK_BOT_TOKEN_HAZBOT)"
    assert (
        chjson["slack"][1]["token"] == "$$secret(SLACK_BOT_TOKEN_SUPPORT_BOT)"
    )


def test_wizard_first_step_collects_bot_name():
    """The first step's field list leads with ``bot_name`` so the UI
    pre-fills it before any other inputs. (Spec 08 D10: the D10 first
    step has no secret fields — it just generates a manifest.)"""

    p = slack_provisioner.SlackProvisioner()
    first_step = p.wizard_steps()[0]
    assert first_step.id == "manifest_config"
    field_ids = [f.id for f in first_step.fields]
    assert field_ids[0] == "bot_name", (
        "bot_name must be the first field so the wizard collects the "
        "local identifier before anything else"
    )


# ---------------------------------------------------------------------------
# Spec 08 D10 — paste-manifest flow (no orphan apps)
# ---------------------------------------------------------------------------


def test_d10_step1_emits_manifest_json_in_message(isolated_env):
    """D10 step 1 (``manifest_config``) generates the manifest, stashes
    it in the session, and surfaces it in the response message for the
    user to copy into Slack's UI. **No Slack API call is made.**"""

    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()

    # Anything calling urlopen would prove an unwanted HTTP call slipped in.
    with patch(
        "urllib.request.urlopen",
        side_effect=AssertionError("D10 step 1 must not hit the network"),
    ):
        result = p.provision(
            "manifest_config",
            {
                "bot_name": "hazbot",
                "display_name": "hyperagent",
                "include_private_channels": True,
                "include_dms": True,
            },
            ctx,
        )

    assert result.error is None
    assert result.next_step == "paste_bot_token"
    # Manifest landed in extra + session for downstream rendering.
    assert "manifest_json" in result.extra
    assert ctx.session.get("manifest_json") == result.extra["manifest_json"]
    # The message includes the JSON body so CLI / minimal UI users still
    # see something to copy without depending on a custom renderer.
    assert "--- MANIFEST JSON ---" in result.message
    parsed = json.loads(result.extra["manifest_json"])
    # display_information.name is the app catalog label (brand string
    # from the manifest builder); bot_user.display_name is the
    # operator-chosen @-handle.
    assert parsed["features"]["bot_user"]["display_name"] == "hyperagent"


def test_d10_step2_validates_and_persists_bot_token(isolated_env):
    """D10 step 2 (``paste_bot_token``) calls auth.test on the pasted
    token. Slack rejection short-circuits with a user-facing error;
    success persists ``SLACK_BOT_TOKEN`` and advances to step 3."""

    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()

    auth_payload = {
        "ok": True,
        "team": "BayesLearner",
        "team_id": "T0123",
        "user": "hyperagent",
        "user_id": "U0456",
        "bot_id": "B0789",
    }
    with patch(
        "urllib.request.urlopen",
        return_value=_mocked_response(auth_payload),
    ):
        result = p.provision(
            "paste_bot_token",
            {"bot_token": "xoxb-real-token"},
            ctx,
        )

    assert result.error is None
    assert result.next_step == "paste_app_token"
    secrets_text = (isolated_env["base"] / "usr" / "secrets.env").read_text()
    assert 'SLACK_BOT_TOKEN="xoxb-real-token"' in secrets_text
    assert 'SLACK_TEAM_ID="T0123"' in secrets_text


def test_d10_step2_rejects_non_xoxb_token(isolated_env):
    """A token that doesn't start with ``xoxb-`` is rejected without
    making an HTTP call — saves the operator an obvious 30-second
    debug cycle."""

    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()
    with patch(
        "urllib.request.urlopen",
        side_effect=AssertionError("invalid token must not hit Slack"),
    ):
        result = p.provision(
            "paste_bot_token",
            {"bot_token": "not-a-real-token"},
            ctx,
        )
    assert result.error is not None
    assert "xoxb-" in result.error
    assert result.next_step is None


def test_d10_step2_surfaces_slack_auth_failure(isolated_env):
    """A typo'd token that LOOKS valid (xoxb- prefix) must be caught
    by the upfront auth.test rather than failing at adapter-start."""

    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()

    with patch(
        "urllib.request.urlopen",
        return_value=_mocked_response({"ok": False, "error": "invalid_auth"}),
    ):
        result = p.provision(
            "paste_bot_token",
            {"bot_token": "xoxb-typo"},
            ctx,
        )
    assert result.error is not None
    assert "invalid_auth" in result.error
    # Token must NOT have been persisted on a rejected validation.
    secrets_text = (isolated_env["base"] / "usr" / "secrets.env").read_text()
    assert "xoxb-typo" not in secrets_text


def test_d10_full_flow_writes_both_tokens_and_channels_block(isolated_env):
    """End-to-end: step 1 (manifest), step 2 (bot token), step 3 (app
    token), step 4 (summary). After step 3 the channels.json block
    exists and references the per-bot secret placeholders."""

    ctx = _make_ctx(isolated_env["channels_path"])
    p = slack_provisioner.SlackProvisioner()

    # Step 1: no HTTP.
    r1 = p.provision(
        "manifest_config",
        {"bot_name": "default", "display_name": "hyperagent"},
        ctx,
    )
    assert r1.next_step == "paste_bot_token"

    # Step 2: auth.test mocked OK.
    with patch(
        "urllib.request.urlopen",
        return_value=_mocked_response(
            {"ok": True, "team": "Bay", "team_id": "T1", "user_id": "U1"}
        ),
    ):
        r2 = p.provision(
            "paste_bot_token", {"bot_token": "xoxb-real"}, ctx
        )
    assert r2.next_step == "paste_app_token"

    # Step 3: app token. No HTTP — the helper just persists.
    r3 = p.provision(
        "paste_app_token", {"app_token": "xapp-real"}, ctx
    )
    assert r3.next_step == "summary"

    secrets_text = (isolated_env["base"] / "usr" / "secrets.env").read_text()
    assert 'SLACK_BOT_TOKEN="xoxb-real"' in secrets_text
    assert 'SLACK_APP_TOKEN="xapp-real"' in secrets_text

    channels = json.loads(isolated_env["channels_path"].read_text())
    # legacy bot_name "default" → dict-shape or single-element list-shape;
    # we accept either as long as the token placeholders land.
    if isinstance(channels.get("slack"), list):
        block = channels["slack"][0]
    else:
        block = channels["slack"]
    assert block["token"] == "$$secret(SLACK_BOT_TOKEN)"
    assert block["app_token"] == "$$secret(SLACK_APP_TOKEN)"
    assert block["enabled"] is True
