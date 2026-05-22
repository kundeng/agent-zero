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
