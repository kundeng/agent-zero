"""Tests for /channels_status per-bot feed (spec 09 task 1.12)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Tmp ``usr/secrets.env`` + tmp ``channels.json`` + reset singletons.

    Mirrors :mod:`tests.test_channels_provision_slack`'s fixture so the
    status feed gets a clean slate without leaking across tests.
    """

    base = tmp_path / "repo"
    (base / "usr").mkdir(parents=True)
    (base / "usr" / "secrets.env").write_text("")

    from python.helpers import files as files_mod

    monkeypatch.setattr(files_mod, "get_base_dir", lambda: str(base))

    from python.helpers.secrets import SecretsManager

    SecretsManager._instances = {}

    channels_path = tmp_path / "channels.json"

    import hyperagent0.channels.channels_config_bridge as bridge_mod
    import hyperagent0.channels.config as config_mod

    monkeypatch.setattr(bridge_mod, "_channels_path", lambda: channels_path)
    monkeypatch.setattr(
        config_mod, "channels_config_file", lambda: channels_path
    )

    return {"base": base, "channels_path": channels_path}


def _write_channels(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def _write_secrets(base: Path, kv: dict[str, str]) -> None:
    lines = [f'{k.upper()}="{v}"' for k, v in kv.items()]
    (base / "usr" / "secrets.env").write_text("\n".join(lines) + "\n")
    # Force secret manager reload.
    from python.helpers.secrets import SecretsManager

    SecretsManager._instances = {}


def _run_status(monkeypatch, live_map: dict | None = None) -> list[dict]:
    """Invoke the ChannelsStatus handler and return its channels list.

    ``live_map`` patches :func:`running_adapters` so tests can simulate
    one/many live bots without standing up the channels runtime.
    """

    import hyperagent0.channels.lifecycle as lifecycle_mod

    monkeypatch.setattr(
        lifecycle_mod, "running_adapters", lambda: dict(live_map or {})
    )

    from python.api.channels_status import ChannelsStatus

    handler = ChannelsStatus.__new__(ChannelsStatus)
    # ApiHandler.__init__ wires Flask state we don't need for process().
    result = asyncio.run(handler.process({}, None))
    assert result["success"] is True
    return result["channels"]


# ---------------------------------------------------------------------------
# Scenario A — no channels.json: every provisioner shows a placeholder
# ---------------------------------------------------------------------------


def test_empty_channels_emits_placeholder_per_provisioner(isolated_env, monkeypatch):
    """No file on disk → one (not configured) row per registered platform."""

    channels = _run_status(monkeypatch)

    by_type = {c["channel_type"]: c for c in channels}
    # Every shipped provisioner registers itself at import time; the
    # placeholder rows expose them.
    assert {"slack", "telegram", "discord"} <= set(by_type.keys())

    for ct in ("slack", "telegram", "discord"):
        row = by_type[ct]
        assert row["bot_name"] == ""
        assert row["configured"] is False
        assert row["enabled"] is False
        assert row["live"] is False
        # required_secrets stays as provisioner-declared (no suffix yet).
        assert row["required_secrets"]
        for key in row["required_secrets"]:
            assert "_" in key or key.isupper()  # SLACK_BOT_TOKEN-style


# ---------------------------------------------------------------------------
# Scenario B — legacy single-bot install still works
# ---------------------------------------------------------------------------


def test_legacy_single_bot_dict_shape_renders_one_row(isolated_env, monkeypatch):
    """Old spec-04 dict-shape channels.json: one bot row, bare secret keys,
    live detection by bare ``channel_type`` key."""

    _write_channels(
        isolated_env["channels_path"],
        {
            "slack": {
                "enabled": True,
                "token": "$$secret(SLACK_BOT_TOKEN)",
                "app_token": "$$secret(SLACK_APP_TOKEN)",
            }
        },
    )
    # Pretend the runtime adapter is live and registered under the bare key
    # (the path running_adapters() takes when bot_name was '_legacy').
    channels = _run_status(monkeypatch, live_map={"slack": {"live": True}})

    slack_rows = [c for c in channels if c["channel_type"] == "slack"]
    assert len(slack_rows) == 1
    row = slack_rows[0]
    # After migration the bot is named 'default', and that name keeps the
    # bare secret keys per the strangler-fig contract.
    assert row["bot_name"] == "default"
    assert row["enabled"] is True
    assert row["required_secrets"] == ["SLACK_APP_ID", "SLACK_TEAM_ID",
                                       "SLACK_SIGNING_SECRET", "SLACK_CLIENT_ID",
                                       "SLACK_CLIENT_SECRET", "SLACK_BOT_TOKEN",
                                       "SLACK_APP_TOKEN"]
    # Live: bare-key fallback because bot_name == 'default'.
    assert row["live"] is True


# ---------------------------------------------------------------------------
# Scenario C — two slack bots → two rows, each with per-bot secret keys
# ---------------------------------------------------------------------------


def test_two_slack_bots_emit_two_rows_with_suffixed_secrets(
    isolated_env, monkeypatch
):
    """Multi-bot list-shape: one row per bot, secrets suffixed with bot name."""

    _write_channels(
        isolated_env["channels_path"],
        {
            "slack": [
                {"name": "hazbot", "enabled": True, "default_project": "engineering"},
                {"name": "support-bot", "enabled": False,
                 "default_project": "customer-ops"},
            ]
        },
    )
    # Pretend only the first bot has live tokens.
    _write_secrets(
        isolated_env["base"],
        {"SLACK_BOT_TOKEN_HAZBOT": "xoxb-1"},
    )

    channels = _run_status(
        monkeypatch,
        live_map={"slack/hazbot": {"live": True}},
    )
    slack_rows = [c for c in channels if c["channel_type"] == "slack"]
    assert [r["bot_name"] for r in slack_rows] == ["hazbot", "support-bot"]

    haz = slack_rows[0]
    support = slack_rows[1]

    # Per-bot suffix on every required secret. 'hazbot' has the bot
    # token configured, so configured_secrets[<that key>] is True.
    assert "SLACK_BOT_TOKEN_HAZBOT" in haz["required_secrets"]
    assert "SLACK_BOT_TOKEN_SUPPORT_BOT" in support["required_secrets"]
    assert haz["configured_secrets"]["SLACK_BOT_TOKEN_HAZBOT"] is True
    assert support["configured_secrets"]["SLACK_BOT_TOKEN_SUPPORT_BOT"] is False

    # Per-bot live state — composite key match.
    assert haz["live"] is True
    assert support["live"] is False

    # Per-bot fields land in the response.
    assert haz["default_project"] == "engineering"
    assert support["default_project"] == "customer-ops"
    assert haz["enabled"] is True
    assert support["enabled"] is False
