"""Channel configuration loader (spec 04, task 1.5).

Spec 04's conflict-surface budget is zero upstream patches, so channel
config does NOT live in ``python/helpers/settings.py``. Instead we look
for, in order of precedence:

1. ``~/.hyperagent0/channels.json`` (preferred — user-writable, ignored
   by the repo).
2. A top-level ``channels`` section inside ``usr/settings.json`` (the
   upstream settings file). Read-only here; we never write back to it.

Both shapes are the same dict:

.. code-block:: jsonc

   {
     "telegram": {
       "enabled": true,
       "token": "$$secret(TELEGRAM_BOT_TOKEN)",
       "allowed_users": ["12345", "67890"],
       "allowed_chats": [],
       "project_binding": {
         "default": "personal",
         "12345": "work"
       }
     },
     "slack": { "enabled": false },
     "discord": { "enabled": false }
   }

``$$secret(KEY)`` and ``§§secret(KEY)`` placeholders inside any string
value are resolved by :func:`resolve_secrets` via the upstream
``SecretsManager``. Resolution happens at the boundary where the
adapter actually needs the cleartext (typically inside ``connect()``)
so the placeholder form stays in memory as long as possible.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def channels_config_file() -> Path:
    """Return the preferred path for ``channels.json``.

    We do not create the file here — :func:`load_channels_config`
    treats a missing file as "no channels configured".
    """

    return Path(os.path.expanduser("~/.hyperagent0/channels.json"))


def _upstream_settings_path() -> Path:
    """Return the path to ``usr/settings.json`` relative to the repo.

    Resolved relative to this file's grandparent (``hyperagent0/`` →
    repo root). Kept local so we don't have to import any upstream
    module just to find a JSON file.
    """

    return Path(__file__).resolve().parents[2] / "usr" / "settings.json"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass
class ChannelConfig:
    """Per-channel config block.

    ``raw`` keeps the original dict (post secret resolution) so adapters
    can pull adapter-specific extras (e.g. Slack app token) without us
    needing a typed field for every platform.

    Spec 06 additions:

    * ``require_mention`` — when True, ignore group/channel messages that
      don't carry a platform-confirmed bot mention (D3). DMs always pass.
      Defaults to False (preserving spec-04 behavior).
    * ``sandbox_override`` — when set, the router forces this sandbox
      mode on contexts created for this channel, regardless of project
      defaults (D5). Same shape as ``ProjectSandboxSettings`` in
      :mod:`python.helpers.projects`.
    """

    name: str
    enabled: bool = False
    token: str = ""
    allowed_users: list[str] = field(default_factory=list)
    allowed_chats: list[str] = field(default_factory=list)
    project_binding: Dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    # ---- spec 06 D3/D5 additions ----
    require_mention: bool = False
    sandbox_override: Optional[Dict[str, Any]] = None

    def is_user_allowed(self, user_id: str) -> bool:
        """True iff the user is allowed to talk to this channel.

        Empty allow-list = open (no restriction). Matches NanoClaw's
        behavior; we keep it permissive by default and trust the caller
        to set the list for public channels.
        """

        if not self.allowed_users:
            return True
        return str(user_id) in {str(u) for u in self.allowed_users}

    def is_chat_allowed(self, chat_id: str) -> bool:
        if not self.allowed_chats:
            return True
        return str(chat_id) in {str(c) for c in self.allowed_chats}

    def project_for_chat(self, chat_id: str) -> Optional[str]:
        """Resolve the project name a fresh context for ``chat_id``
        should activate, or ``None`` if no binding applies.

        Lookup order:
          1. ``project_binding[chat_id]`` (exact match)
          2. ``project_binding["default"]`` (catch-all)
        """

        if not self.project_binding:
            return None
        return self.project_binding.get(str(chat_id)) or self.project_binding.get(
            "default"
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _coerce_channel_dict(name: str, raw: Any) -> Optional[ChannelConfig]:
    if not isinstance(raw, dict):
        return None
    sandbox_raw = raw.get("sandbox_override")
    sandbox_override = sandbox_raw if isinstance(sandbox_raw, dict) else None
    return ChannelConfig(
        name=name,
        enabled=bool(raw.get("enabled", False)),
        token=str(raw.get("token", "") or ""),
        allowed_users=list(raw.get("allowed_users", []) or []),
        allowed_chats=list(raw.get("allowed_chats", []) or []),
        project_binding=dict(raw.get("project_binding", {}) or {}),
        raw=raw,
        require_mention=bool(raw.get("require_mention", False)),
        sandbox_override=sandbox_override,
    )


def _read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def load_channels_config(
    *,
    channels_json: Optional[Path] = None,
    settings_json: Optional[Path] = None,
) -> Dict[str, ChannelConfig]:
    """Load the channel config map.

    Parameters
    ----------
    channels_json
        Override path for the dedicated channels file (test seam).
    settings_json
        Override path for the upstream settings file (test seam).

    Returns
    -------
    Mapping of channel name → :class:`ChannelConfig`. Channels not
    present in the config file are simply absent from the dict.
    """

    out: Dict[str, ChannelConfig] = {}

    # Layer 1: upstream settings.json (lowest priority).
    settings_path = settings_json or _upstream_settings_path()
    settings_data = _read_json(settings_path) or {}
    settings_channels = settings_data.get("channels")
    if isinstance(settings_channels, dict):
        for name, raw in settings_channels.items():
            cfg = _coerce_channel_dict(name, raw)
            if cfg is not None:
                out[name] = cfg

    # Layer 2: dedicated ~/.hyperagent0/channels.json (overrides).
    cj_path = channels_json or channels_config_file()
    cj_data = _read_json(cj_path) or {}
    for name, raw in cj_data.items():
        cfg = _coerce_channel_dict(name, raw)
        if cfg is not None:
            out[name] = cfg

    return out


# ---------------------------------------------------------------------------
# Secret resolution
# ---------------------------------------------------------------------------


def resolve_secret(text: str) -> str:
    """Resolve ``$$secret(KEY)`` / ``§§secret(KEY)`` placeholders in ``text``.

    Uses upstream :class:`python.helpers.secrets.SecretsManager`. We
    import lazily so this module stays usable in tests that don't have
    the secrets store wired up — those tests simply pass cleartext.
    """

    if not text:
        return text
    try:
        from python.helpers.secrets import SecretsManager  # type: ignore

        mgr = SecretsManager.get_instance()
        return mgr.replace_placeholders(text)
    except Exception:
        # If the upstream resolver is missing or misconfigured, fall
        # back to the raw text. Adapters that need a real secret will
        # fail loudly when they hand the placeholder to their SDK.
        return text
