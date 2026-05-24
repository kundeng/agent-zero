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

    Spec 06 addition:

    * ``require_mention`` — when True, ignore group/channel messages that
      don't carry a platform-confirmed bot mention (D3). DMs always pass.
      Defaults to False (preserving spec-04 behavior).
    """

    name: str
    enabled: bool = False
    token: str = ""
    allowed_users: list[str] = field(default_factory=list)
    allowed_chats: list[str] = field(default_factory=list)
    project_binding: Dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    require_mention: bool = False

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
    return ChannelConfig(
        name=name,
        enabled=bool(raw.get("enabled", False)),
        token=str(raw.get("token", "") or ""),
        allowed_users=list(raw.get("allowed_users", []) or []),
        allowed_chats=list(raw.get("allowed_chats", []) or []),
        project_binding=dict(raw.get("project_binding", {}) or {}),
        raw=raw,
        require_mention=bool(raw.get("require_mention", False)),
    )


# ---------------------------------------------------------------------------
# Multi-bot schema (spec 09 D1) — list-of-bots per platform
# ---------------------------------------------------------------------------


@dataclass
class BotConfig:
    """One bot instance under a platform (spec 09 D1).

    Multiple BotConfigs can exist for the same platform; each represents
    a distinct bot identity (different tokens, possibly different
    default project).

    ``bot_name`` is the local identifier the operator chose. It's
    surfaced in logs and the Channels UI but is NOT the display name
    Slack/Telegram/Discord shows users — that's set in the platform's
    own app config.
    """

    channel_type: str  # "slack" / "telegram" / "discord"
    bot_name: str       # operator-chosen local id
    enabled: bool = False
    token: str = ""
    app_token: str = ""  # slack only; ignored by others
    default_project: str = ""  # empty → _default project (spec 09 D2)
    project_overrides: Dict[str, str] = field(default_factory=dict)
    allowed_users: list[str] = field(default_factory=list)
    allowed_chats: list[str] = field(default_factory=list)
    require_mention: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def project_for_chat(self, chat_id: str) -> str:
        """Resolve the project name for ``chat_id``.

        Lookup order:
          1. ``project_overrides[chat_id]`` (exact match)
          2. ``default_project`` (or "_default" if empty)
        """

        override = self.project_overrides.get(str(chat_id))
        if override:
            return override
        from hyperagent0.projects import resolve_project_name

        return resolve_project_name(self.default_project)


def _coerce_bot_dict(channel_type: str, raw: Any, *, fallback_name: str) -> Optional[BotConfig]:
    """Coerce one bot entry. ``fallback_name`` used when ``raw`` has no name."""

    if not isinstance(raw, dict):
        return None
    return BotConfig(
        channel_type=channel_type,
        bot_name=str(raw.get("name") or fallback_name),
        enabled=bool(raw.get("enabled", False)),
        token=str(raw.get("token", "") or ""),
        app_token=str(raw.get("app_token", "") or ""),
        default_project=str(raw.get("default_project", "") or ""),
        project_overrides=dict(
            raw.get("project_overrides", raw.get("project_binding", {})) or {}
        ),
        allowed_users=list(raw.get("allowed_users", []) or []),
        allowed_chats=list(raw.get("allowed_chats", []) or []),
        require_mention=bool(raw.get("require_mention", False)),
        raw=raw,
    )


def load_bot_configs(
    *,
    channels_json: Optional[Path] = None,
    settings_json: Optional[Path] = None,
    persist_migration: bool = True,
) -> Dict[str, list[BotConfig]]:
    """Load the channel config map in spec-09 list-of-bots shape.

    Backward-compat: if ``~/.hyperagent0/channels.json`` is in
    dict-shape per platform (the spec-04 / spec-08 schema), the loader
    auto-wraps each entry as a single-element list under bot name
    ``default``. When ``persist_migration`` is True (the default), the
    normalized list-shape is also written back to disk so subsequent
    loads, hand-edits, and provisioning UX all see the new schema.
    Settings.json is never modified — that's the upstream read-only
    layer.

    Existing callers using :func:`load_channels_config` continue to
    work — that function returns the first bot per platform, which is
    correct for single-bot installs until lifecycle migrates to
    :func:`load_bot_configs` directly.
    """

    out: Dict[str, list[BotConfig]] = {}

    def _ingest(channel_type: str, value: Any) -> None:
        if isinstance(value, dict):
            # Old single-bot shape: { "slack": { token: "...", ... } }.
            bot = _coerce_bot_dict(channel_type, value, fallback_name="default")
            if bot is not None:
                out.setdefault(channel_type, []).append(bot)
        elif isinstance(value, list):
            # New multi-bot shape: { "slack": [ {name, token, ...}, ... ] }.
            for i, raw in enumerate(value):
                bot = _coerce_bot_dict(
                    channel_type, raw, fallback_name=f"bot{i}"
                )
                if bot is not None:
                    out.setdefault(channel_type, []).append(bot)

    settings_path = settings_json or _upstream_settings_path()
    settings_data = _read_json(settings_path) or {}
    settings_channels = settings_data.get("channels")
    if isinstance(settings_channels, dict):
        for channel_type, value in settings_channels.items():
            _ingest(channel_type, value)

    cj_path = channels_json or channels_config_file()
    cj_data = _read_json(cj_path) or {}
    # ~/.hyperagent0/channels.json overrides: completely replace per-
    # platform entries from settings.json (the layering matches
    # load_channels_config).
    for channel_type, value in cj_data.items():
        if channel_type in out:
            out[channel_type] = []
        _ingest(channel_type, value)

    if persist_migration and cj_data:
        _maybe_persist_normalized(cj_path, cj_data)

    return out


def _maybe_persist_normalized(cj_path: Path, cj_data: dict) -> None:
    """If ``cj_data`` has any dict-shape entry, write the normalized
    list-shape back to ``cj_path``.

    Idempotent: a file already in list-shape is left untouched (the
    same bytes would be written, but we avoid the disk write so file
    mtime stays stable for tools that watch it).

    Atomic-ish: writes to a tempfile sibling and renames into place,
    so a crash mid-write doesn't leave a half-written channels.json.
    Other failure modes (filesystem unwritable, permission denied) are
    swallowed with a log entry — config load must succeed even when
    we can't persist the migration.
    """

    needs_migration = any(isinstance(v, dict) for v in cj_data.values())
    if not needs_migration:
        return

    normalized: dict[str, list[dict]] = {}
    for channel_type, value in cj_data.items():
        if isinstance(value, dict):
            entry = dict(value)
            entry.setdefault("name", "default")
            normalized[channel_type] = [entry]
        elif isinstance(value, list):
            normalized[channel_type] = [dict(v) for v in value if isinstance(v, dict)]
        # Anything else: drop. Garbage in the file shouldn't be preserved.

    try:
        cj_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cj_path.with_suffix(cj_path.suffix + ".tmp")
        tmp.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, cj_path)
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).exception(
            "could not persist normalized channels.json to %s", cj_path
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
