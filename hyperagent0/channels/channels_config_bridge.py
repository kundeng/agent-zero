"""Bridge to ``~/.hyperagent0/channels.json`` (spec 08 D6+D7).

Provisioners and the generic Flask handlers update the per-channel
config through this module rather than touching the JSON file
directly. The bridge owns three guarantees:

1. **Atomic writes.** Any change goes through a tempfile + ``os.replace``
   pair, so a crashed daemon or pulled power cable never leaves a
   half-written ``channels.json`` for the next boot to choke on.

2. **Same shape as the loader.** :func:`hyperagent0.channels.config.load_channels_config`
   is the source of truth for the on-disk schema. The bridge produces
   files that loader reads cleanly, including the optional fields
   (``project_binding``, ``allowed_users``, ``allowed_chats``,
   ``require_mention``).

3. **No secrets in plaintext.** Per spec 08 D6, the bridge does not
   inject token-shaped values — it only ever writes whatever block
   the caller hands it. Provisioners are responsible for building
   blocks that reference secrets via ``$$secret(NAME)``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


def _channels_path() -> Path:
    """Return the canonical path for ``~/.hyperagent0/channels.json``.

    Imported from :mod:`hyperagent0.channels.config` so the bridge and
    the loader never disagree on where the file lives.
    """

    # Lazy import to keep the module import cheap (spec 03 D5).
    from .config import channels_config_file

    return channels_config_file()


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Malformed file — defer to whoever cares (the loader returns
        # an empty config in that case). The bridge will write a clean
        # file the moment it has new content to land.
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` as pretty-printed JSON via tempfile + os.replace.

    ``tempfile.NamedTemporaryFile`` creates the temp file in the same
    directory as the target so ``os.replace`` is a rename within the
    same filesystem (atomic on POSIX; close-to-atomic on Windows).
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd_tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(data, fd_tmp, indent=2, sort_keys=True)
        fd_tmp.write("\n")
        fd_tmp.flush()
        os.fsync(fd_tmp.fileno())
        fd_tmp.close()
        os.replace(fd_tmp.name, path)
    except Exception:
        # Clean up the tempfile on any failure so we don't litter the
        # config directory with abandoned ``.tmp`` files.
        try:
            os.unlink(fd_tmp.name)
        except OSError:
            pass
        raise


class FileChannelsConfigBridge:
    """Concrete :class:`ChannelsConfigBridge` backed by the JSON file.

    Tests that need isolation can override the path via ``path`` —
    callers in the daemon use the default.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _channels_path()

    # ------------------------------------------------------------------
    # ChannelsConfigBridge protocol implementation
    # ------------------------------------------------------------------

    def read_block(self, channel_type: str) -> dict[str, Any]:
        data = _read(self._path)
        raw = data.get(channel_type, {})
        return raw if isinstance(raw, dict) else {}

    def update_block(self, channel_type: str, block: dict[str, Any]) -> None:
        """Replace the entire per-channel block.

        Provisioners typically read the existing block first, merge
        their changes locally, and write back. The bridge does not
        deep-merge — that's the caller's responsibility.
        """

        data = _read(self._path)
        data[channel_type] = dict(block)
        _write_atomic(self._path, data)

    # ------------------------------------------------------------------
    # Multi-bot helpers (spec 09 task 1.12 + 1.13)
    # ------------------------------------------------------------------

    def list_bot_names(self, channel_type: str) -> list[str]:
        """Return the names of every bot currently configured for ``channel_type``.

        Used by the wizard to suggest a unique default bot_name when
        the operator adds another bot. Honors both schema shapes:
        dict-shape legacy entries surface as ``["default"]``;
        list-shape entries return their declared names.
        """

        data = _read(self._path)
        value = data.get(channel_type)
        if isinstance(value, dict):
            return [str(value.get("name") or "default")]
        if isinstance(value, list):
            out: list[str] = []
            for i, entry in enumerate(value):
                if isinstance(entry, dict):
                    out.append(str(entry.get("name") or f"bot{i}"))
            return out
        return []

    def read_bot_block(self, channel_type: str, bot_name: str) -> dict[str, Any]:
        """Return the block for one named bot, or ``{}`` if absent.

        Normalizes the dict-shape legacy schema on the fly: a dict-shape
        ``channels.json`` is treated as if its bot were named
        ``"default"``. Pure read — never mutates disk.
        """

        data = _read(self._path)
        value = data.get(channel_type)
        if isinstance(value, dict):
            existing_name = str(value.get("name") or "default")
            if existing_name == bot_name or bot_name in ("", "default"):
                return dict(value)
            return {}
        if isinstance(value, list):
            for i, entry in enumerate(value):
                if not isinstance(entry, dict):
                    continue
                entry_name = str(entry.get("name") or f"bot{i}")
                if entry_name == bot_name:
                    return dict(entry)
        return {}

    def set_bot_block(
        self,
        channel_type: str,
        bot_name: str,
        block: dict[str, Any],
    ) -> None:
        """Upsert ``block`` under ``bot_name`` inside the platform's list.

        Normalizes the on-disk shape to spec 09 list-of-bots as a side
        effect: a dict-shape entry is wrapped into a single-element list
        keyed by its own ``name`` (or ``"default"``) before the upsert
        runs. The block is written with ``name`` set to ``bot_name`` so
        :func:`hyperagent0.channels.config.load_bot_configs` reads it
        back correctly without further migration.

        Atomic-rename write — same guarantees as :meth:`update_block`.
        """

        if not bot_name:
            # Empty name is the strangler-fig legacy signal — fall
            # through to the older single-bot writer so behavior matches
            # spec-04 callers that never collected a bot_name.
            self.update_block(channel_type, block)
            return

        data = _read(self._path)
        value = data.get(channel_type)

        # Normalize current value to a list-of-bots regardless of shape.
        bots: list[dict[str, Any]]
        if isinstance(value, dict):
            wrapped = dict(value)
            wrapped.setdefault("name", "default")
            bots = [wrapped]
        elif isinstance(value, list):
            bots = [dict(e) for e in value if isinstance(e, dict)]
        else:
            bots = []

        # Stamp the name so the loader's fallback_name logic never has
        # to guess. Upsert by name (case-sensitive — matches
        # secret_key_for_bot's normalization).
        new_entry = dict(block)
        new_entry["name"] = bot_name

        replaced = False
        for i, existing in enumerate(bots):
            if str(existing.get("name") or f"bot{i}") == bot_name:
                bots[i] = new_entry
                replaced = True
                break
        if not replaced:
            bots.append(new_entry)

        data[channel_type] = bots
        _write_atomic(self._path, data)

    def update_project_binding(
        self,
        channel_type: str,
        *,
        chat_id: Optional[str],
        project_name: Optional[str],
    ) -> None:
        """Set or clear one entry in the channel's ``project_binding`` map.

        ``chat_id=None`` writes the ``"default"`` entry; otherwise the
        per-chat key. ``project_name=None`` deletes the entry.

        Touches only ``project_binding`` — the rest of the channel's
        block survives unchanged.
        """

        data = _read(self._path)
        block = data.get(channel_type)
        if not isinstance(block, dict):
            block = {}

        binding = block.get("project_binding")
        if not isinstance(binding, dict):
            binding = {}

        key = "default" if chat_id is None else str(chat_id)
        if project_name is None:
            binding.pop(key, None)
        else:
            binding[key] = str(project_name)

        block["project_binding"] = binding
        data[channel_type] = block
        _write_atomic(self._path, data)
