"""Channel provisioning framework (spec 08).

This package is the *operator UX* layer that sits on top of the
spec-04 runtime (``hyperagent0/channels/{base,router,slack,...}``).

The runtime tells the daemon how to send/receive messages once tokens
exist. The provisioning framework is how those tokens land in
``usr/secrets.env`` and ``~/.hyperagent0/channels.json`` in the first
place — driven by a declarative wizard that the Web UI and the
``haz channel`` CLI both consume.

The package mirrors the shape of :mod:`hyperagent0.channels` itself:

* :class:`BaseProvisioner` is the abstract base; concrete provisioners
  (``slack.SlackProvisioner``, ``telegram.TelegramProvisioner``,
  ``discord.DiscordProvisioner``) inherit from it and call
  :func:`register_provisioner` at import time.
* :func:`get_provisioner` / :func:`registered_provisioners` resolve at
  runtime — the Flask handlers and CLI dispatch on ``channel_type``.

Importing this package MUST stay cheap. Heavy SDK imports
(``slack_sdk``, ``python-telegram-bot``, ``discord.py``) are NOT done
here. Each per-platform module imports its own HTTP helpers only when
its provisioner methods actually run — and the provisioners use
stdlib ``urllib.request`` rather than the runtime adapters' SDKs, so
provisioning works even when those SDKs are not installed.
"""

from __future__ import annotations

from .base import (
    BaseProvisioner,
    ChannelsConfigBridge,
    ProvisionContext,
    SecretsBridge,
    StepResult,
    WizardField,
    WizardStep,
    get_provisioner,
    register_provisioner,
    registered_provisioners,
    step_result_to_json,
)

__all__ = [
    "BaseProvisioner",
    "ChannelsConfigBridge",
    "ProvisionContext",
    "SecretsBridge",
    "StepResult",
    "WizardField",
    "WizardStep",
    "get_provisioner",
    "register_provisioner",
    "registered_provisioners",
    "step_result_to_json",
]
