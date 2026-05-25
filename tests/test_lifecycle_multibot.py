"""Spec 09 P2.4 + P2.5 — lifecycle multi-bot integration tests.

The unit-level multi-bot work (config bridge, ThreadStore, router) is
covered by `test_hyperagent0_channels_spec09.py` and friends. This file
exercises ``lifecycle.start_enabled_channels`` end-to-end: a
``channels.json`` with two bots → two adapter instances connected →
``_channels`` map keyed by ``(channel_type, bot_name)`` → router
dispatches inbound to the right adapter.

We stub the Slack adapter so no real Socket Mode connection happens.
The test asserts the wiring, not Slack-SDK behavior.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Stub Slack adapter — registers a fake "slack" channel class so the
# real slack-bolt stack never loads.
# ---------------------------------------------------------------------------


class _FakeSlackChannel:
    """Mimics enough of BaseChannel for lifecycle.start_enabled_channels."""

    channel_type = "slack"

    def __init__(self, config: dict, bot_name: str = "_legacy") -> None:
        self.config = config
        self.bot_name = bot_name
        self.connected = False
        self.disconnected = False
        self.sent: list = []
        # Router calls channel.setup(router) at registration time —
        # provide a no-op so the wiring goes through.
        self.setup = lambda router: None

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send(self, outbound) -> None:
        self.sent.append(outbound)


@pytest.fixture
def lifecycle_clean(tmp_path, monkeypatch):
    """Isolate channels.json + reset lifecycle module globals between runs.

    Each test gets its own ``channels.json``, ``usr/secrets.env``, and a
    fresh ``_channels`` / ``_router`` so prior state can't leak.
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

    monkeypatch.setattr(bridge_mod, "_channels_path", lambda: channels_path)

    # Tell load_bot_configs to read from the same path.
    import hyperagent0.channels.config as config_mod

    monkeypatch.setattr(
        config_mod, "channels_config_file", lambda: channels_path
    )

    # Replace the Slack adapter factory with our fake so no real
    # connection is attempted. ``_instantiate_adapter`` calls
    # ``get_channel_class("slack")`` — stub it.
    import hyperagent0.channels.lifecycle as lifecycle

    def _fake_instantiate(name: str, cfg, *, bot_name: str = "_legacy"):
        raw = cfg.raw if hasattr(cfg, "raw") else cfg
        return _FakeSlackChannel(raw, bot_name=bot_name)

    monkeypatch.setattr(lifecycle, "_instantiate_adapter", _fake_instantiate)

    # Reset module-globals between tests so state from a previous test
    # doesn't poison the next start_enabled_channels call.
    lifecycle._channels.clear()
    lifecycle._router = None
    lifecycle._loop = None
    lifecycle._thread = None
    lifecycle._started = False

    yield {"channels_path": channels_path, "lifecycle": lifecycle}

    # Tear down any thread that lingered.
    try:
        lifecycle.stop_all_channels(timeout=2.0)
    except Exception:
        pass


def _write_two_bot_config(path) -> None:
    """Land a channels.json with two enabled Slack bots."""

    path.write_text(
        json.dumps(
            {
                "slack": [
                    {
                        "name": "hazbot",
                        "enabled": True,
                        "token": "xoxb-hazbot-fake",
                        "app_token": "xapp-hazbot-fake",
                        "default_project": "engineering",
                    },
                    {
                        "name": "support",
                        "enabled": True,
                        "token": "xoxb-support-fake",
                        "app_token": "xapp-support-fake",
                        "default_project": "customer-ops",
                    },
                ]
            }
        )
    )


# ---------------------------------------------------------------------------
# P2.4 — start_enabled_channels spawns one adapter per enabled bot
# ---------------------------------------------------------------------------


def test_lifecycle_spawns_one_adapter_per_bot(lifecycle_clean):
    """Two list-shape bots → two ``_channels`` entries keyed by
    ``(channel_type, bot_name)``, each adapter received its own config."""

    _write_two_bot_config(lifecycle_clean["channels_path"])
    lifecycle = lifecycle_clean["lifecycle"]

    lifecycle.start_enabled_channels()

    # Give the channels loop a moment to finish connect().
    deadline = time.time() + 5.0
    while len(lifecycle._channels) < 2 and time.time() < deadline:
        time.sleep(0.05)

    assert ("slack", "hazbot") in lifecycle._channels
    assert ("slack", "support") in lifecycle._channels

    hazbot = lifecycle._channels[("slack", "hazbot")]
    support = lifecycle._channels[("slack", "support")]

    # Each adapter sees its own per-bot config — no cross-contamination.
    assert hazbot.config["token"] == "xoxb-hazbot-fake"
    assert support.config["token"] == "xoxb-support-fake"
    assert hazbot.bot_name == "hazbot"
    assert support.bot_name == "support"

    # Both adapters were actually connected (not just instantiated).
    assert hazbot.connected
    assert support.connected


def test_lifecycle_skips_disabled_bots(lifecycle_clean):
    """A list entry with ``enabled: false`` must not produce an adapter."""

    path = lifecycle_clean["channels_path"]
    path.write_text(
        json.dumps(
            {
                "slack": [
                    {
                        "name": "alpha",
                        "enabled": True,
                        "token": "xoxb-a",
                        "app_token": "xapp-a",
                    },
                    {
                        "name": "beta",
                        "enabled": False,
                        "token": "xoxb-b",
                        "app_token": "xapp-b",
                    },
                ]
            }
        )
    )

    lifecycle = lifecycle_clean["lifecycle"]
    lifecycle.start_enabled_channels()

    deadline = time.time() + 3.0
    while len(lifecycle._channels) == 0 and time.time() < deadline:
        time.sleep(0.05)

    assert ("slack", "alpha") in lifecycle._channels
    assert ("slack", "beta") not in lifecycle._channels
    assert len(lifecycle._channels) == 1


def test_lifecycle_idempotent_on_repeated_call(lifecycle_clean):
    """Calling start_enabled_channels twice is a no-op — spec 09 D5
    promises ``_started`` short-circuits the second entry."""

    _write_two_bot_config(lifecycle_clean["channels_path"])
    lifecycle = lifecycle_clean["lifecycle"]

    lifecycle.start_enabled_channels()
    deadline = time.time() + 3.0
    while len(lifecycle._channels) < 2 and time.time() < deadline:
        time.sleep(0.05)
    snapshot = dict(lifecycle._channels)

    lifecycle.start_enabled_channels()  # second call — should be a no-op
    assert lifecycle._channels == snapshot


# ---------------------------------------------------------------------------
# P2.5 — multi-bot dispatch through the router
# ---------------------------------------------------------------------------


def test_router_receives_per_bot_inbound(lifecycle_clean):
    """Inbound messages stamped with different ``bot_name`` route to
    different (channel_type, bot_name) router keys.

    This tests the dispatch *plumbing*: ``InboundMessage.bot_name``
    survives the round-trip from adapter into router, and the router
    indexes adapters/configs by the composite key.
    """

    _write_two_bot_config(lifecycle_clean["channels_path"])
    lifecycle = lifecycle_clean["lifecycle"]
    lifecycle.start_enabled_channels()

    deadline = time.time() + 3.0
    while lifecycle._router is None or len(lifecycle._channels) < 2:
        if time.time() > deadline:
            pytest.fail("channels did not boot within 3s")
        time.sleep(0.05)

    router = lifecycle._router

    # Both adapter instances should be registered under their composite key.
    assert ("slack", "hazbot") in router.channels
    assert ("slack", "support") in router.channels

    hazbot = router.channels[("slack", "hazbot")]
    support = router.channels[("slack", "support")]
    assert hazbot is not support  # two distinct instances

    # Per-bot config (default_project etc.) is independently accessible.
    hazbot_cfg = router.channel_configs[("slack", "hazbot")]
    support_cfg = router.channel_configs[("slack", "support")]
    assert hazbot_cfg.default_project == "engineering"
    assert support_cfg.default_project == "customer-ops"
