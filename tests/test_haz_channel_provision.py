"""Tests for the ``haz channel`` CLI shim (spec 08 task 2.10).

The CLI is a thin wrapper around the same dispatch helpers used by
the Flask handlers. These tests pin the surface behaviors:
``list`` / ``status`` / ``provision --show-steps`` / ``provision``
with --input flags. HTTP-driving tests for the Slack flow itself
are in ``test_channels_provision_slack.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from hyperagent0.cli_commands import channel as channel_cmd


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


def test_channel_list_shows_all_three(isolated_env):
    runner = CliRunner()
    result = runner.invoke(channel_cmd.command, ["list"])
    assert result.exit_code == 0, result.output
    assert "slack" in result.output
    assert "telegram" in result.output
    assert "discord" in result.output
    assert "https://api.slack.com/apps" in result.output


def test_channel_status_not_configured_initially(isolated_env):
    runner = CliRunner()
    result = runner.invoke(channel_cmd.command, ["status"])
    assert result.exit_code == 0, result.output
    assert "not configured" in result.output


def test_channel_status_json_mode(isolated_env):
    runner = CliRunner()
    result = runner.invoke(channel_cmd.command, ["status", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "channels" in data
    types = {c["channel_type"] for c in data["channels"]}
    assert {"slack", "telegram", "discord"}.issubset(types)


def test_provision_show_steps_for_slack(isolated_env):
    runner = CliRunner()
    result = runner.invoke(
        channel_cmd.command, ["provision", "slack", "--show-steps"]
    )
    assert result.exit_code == 0, result.output
    assert "config_token" in result.output
    assert "install" in result.output
    assert "app_token" in result.output


def test_provision_show_steps_for_telegram(isolated_env):
    runner = CliRunner()
    result = runner.invoke(
        channel_cmd.command, ["provision", "telegram", "--show-steps"]
    )
    assert result.exit_code == 0, result.output
    assert "bot_token" in result.output
    assert "allowed_users" in result.output


def test_provision_show_steps_for_discord(isolated_env):
    runner = CliRunner()
    result = runner.invoke(
        channel_cmd.command, ["provision", "discord", "--show-steps"]
    )
    assert result.exit_code == 0, result.output
    assert "credentials" in result.output
    assert "application_id" in result.output


def test_provision_unknown_platform(isolated_env):
    runner = CliRunner()
    result = runner.invoke(
        channel_cmd.command, ["provision", "totally-not-real"]
    )
    assert result.exit_code != 0


def test_provision_telegram_with_inputs(isolated_env):
    """Run the actual provision flow (mocked HTTP) end-to-end via CLI."""

    from hyperagent0.channels.provision import telegram as tg

    runner = CliRunner()
    with patch.object(
        tg.urllib.request, "urlopen",
        return_value=_mocked_response({
            "ok": True, "result": {"username": "cli_test_bot"},
        }),
    ):
        result = runner.invoke(
            channel_cmd.command,
            [
                "provision", "telegram",
                "--input", "bot_token=12345:abc",
                "--non-interactive",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "Provisioning complete" in result.output

    env_text = (isolated_env["base"] / "usr" / "secrets.env").read_text()
    assert 'TELEGRAM_BOT_TOKEN="12345:abc"' in env_text


def test_provision_non_interactive_missing_required_field_errors(isolated_env):
    runner = CliRunner()
    # Telegram needs bot_token; we don't supply it.
    result = runner.invoke(
        channel_cmd.command,
        ["provision", "telegram", "--non-interactive"],
    )
    assert result.exit_code != 0
    assert "bot_token" in result.output.lower()


def test_input_flag_requires_equals_sign(isolated_env):
    runner = CliRunner()
    result = runner.invoke(
        channel_cmd.command,
        [
            "provision", "telegram",
            "--input", "no-equals-here",
            "--non-interactive",
        ],
    )
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output


def test_apply_is_noop_on_idle_daemon():
    runner = CliRunner()
    # restart_channels() in lifecycle is a safe no-op when no channels
    # are started, so apply just prints its success line.
    result = runner.invoke(channel_cmd.command, ["apply"])
    assert result.exit_code == 0, result.output
    assert "restarted" in result.output.lower() or "idle" in result.output.lower()
