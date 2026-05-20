"""Translate filesystem paths between the agent's mount namespace and the host.

When the agent runs inside a container (legacy ``DEPLOYMENT_MODE=docker``)
and spawns a sandbox container via the host Docker daemon (docker-in-docker
through a mounted socket), bind mounts must reference **host** paths. The
agent's view of, say, ``/workspace/projects/foo`` may correspond to
``/var/lib/agent/projects/foo`` on the host.

This module parses ``/proc/self/mountinfo`` to learn the agent's current
mount layout and translate paths in both directions.

The parser is the only piece tested without root/Docker (see
``tests/test_path_translate.py``).

Mountinfo format reference: ``man 5 proc`` — each line has 11+ space-separated
fields. The relevant ones are:

- field 4: root of the mount within the filesystem (host-side path)
- field 5: mount point (agent-side path)
- field 9: filesystem type (e.g. ``ext4``, ``overlay``)
- field 10: mount source (e.g. ``/dev/sda1``)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)


_MOUNTINFO_PATH = "/proc/self/mountinfo"


@dataclass(frozen=True)
class MountEntry:
    """Subset of a ``/proc/self/mountinfo`` line we care about."""

    #: Path inside the source filesystem (host view).
    root: str
    #: Path inside the current mount namespace (agent view).
    mount_point: str
    #: e.g. ``ext4``, ``overlay``, ``tmpfs``.
    fs_type: str
    #: Source device or label (e.g. ``/dev/sda1``).
    source: str


def parse_mountinfo(text: str) -> List[MountEntry]:
    """Parse the contents of ``/proc/self/mountinfo``.

    Tolerates malformed lines (skips them with a debug log). Field positions
    are stable in the kernel's format; the ``-`` separator between optional
    fields and the final triple ``fs_type source super_opts`` is required.
    """
    entries: List[MountEntry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Split on the ``-`` separator; everything before is positional,
        # after is ``<fs_type> <source> <super_opts>``.
        try:
            left, right = line.split(" - ", 1)
        except ValueError:
            logger.debug("mountinfo line missing ' - ' separator: %r", line)
            continue
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or len(right_fields) < 2:
            logger.debug("mountinfo line too short: %r", line)
            continue
        root = left_fields[3]
        mount_point = left_fields[4]
        fs_type = right_fields[0]
        source = right_fields[1]
        entries.append(
            MountEntry(
                root=root,
                mount_point=mount_point,
                fs_type=fs_type,
                source=source,
            )
        )
    return entries


def _read_mountinfo() -> List[MountEntry]:
    try:
        with open(_MOUNTINFO_PATH, "r", encoding="utf-8") as fh:
            return parse_mountinfo(fh.read())
    except FileNotFoundError:
        # Non-Linux or restricted env — degrade to identity mapping.
        return []
    except OSError:
        logger.debug("failed to read %s", _MOUNTINFO_PATH, exc_info=True)
        return []


def _normalize(path: str) -> str:
    """Return an absolute, ``..``-free path without forcing real path resolution.

    We avoid ``os.path.realpath`` because the agent-side path may not exist
    on the host and vice versa.
    """
    if not path:
        return path
    return os.path.normpath(os.path.abspath(path))


def _path_under(path: str, prefix: str) -> bool:
    """True iff ``path`` equals ``prefix`` or is a strict subdirectory."""
    if not prefix:
        return False
    if prefix == "/":
        # Every absolute path is "under /" — but we only want this to match
        # when no more-specific mount applies. Callers rank by prefix length
        # so this works out naturally.
        return path.startswith("/")
    norm = prefix.rstrip("/")
    return path == norm or path == prefix or path.startswith(norm + "/")


def _best_mount_for_agent_path(path: str, mounts: Iterable[MountEntry]) -> Optional[MountEntry]:
    """Pick the longest mount_point that is a prefix of ``path``."""
    best: Optional[MountEntry] = None
    best_len = -1
    for entry in mounts:
        if _path_under(path, entry.mount_point):
            if len(entry.mount_point) > best_len:
                best = entry
                best_len = len(entry.mount_point)
    return best


def _best_mount_for_host_path(path: str, mounts: Iterable[MountEntry]) -> Optional[MountEntry]:
    """Pick the longest mount whose ``root`` is a prefix of ``path``.

    Skips entries where ``root`` is ``/`` and ``mount_point`` is not, because
    those represent pseudo-filesystems (sysfs, proc, devpts) whose ``root``
    field nominally covers every absolute path. We only want to match real
    bind mounts where ``root`` is a meaningful host-side prefix.
    """
    best: Optional[MountEntry] = None
    best_len = -1
    for entry in mounts:
        # Skip pseudo-fs mounts: root="/" but mount_point is something else.
        if entry.root == "/" and entry.mount_point != "/":
            continue
        if _path_under(path, entry.root):
            if len(entry.root) > best_len:
                best = entry
                best_len = len(entry.root)
    return best


def to_host(path: str, mountinfo: Optional[List[MountEntry]] = None) -> str:
    """Map an agent-side path to the equivalent host-side path.

    Identity transform on hosts where ``/proc/self/mountinfo`` is unavailable
    or where no mount entry covers ``path`` (the safe fallback).

    ``mountinfo`` is injected by tests; production callers pass ``None``.
    """
    norm = _normalize(path)
    mounts = mountinfo if mountinfo is not None else _read_mountinfo()
    if not mounts:
        return norm
    entry = _best_mount_for_agent_path(norm, mounts)
    if entry is None or entry.mount_point == entry.root:
        return norm
    # Strip the mount_point prefix, prepend the root.
    mp = entry.mount_point.rstrip("/")
    if norm == entry.mount_point:
        remainder = ""
    else:
        remainder = norm[len(mp):] if mp else norm
    host = (entry.root.rstrip("/") + remainder) or "/"
    return host


def from_host(path: str, mountinfo: Optional[List[MountEntry]] = None) -> str:
    """Inverse of :func:`to_host`. Maps host-side path back to agent view.

    Identity transform when no mount covers ``path``.
    """
    norm = _normalize(path)
    mounts = mountinfo if mountinfo is not None else _read_mountinfo()
    if not mounts:
        return norm
    entry = _best_mount_for_host_path(norm, mounts)
    if entry is None or entry.mount_point == entry.root:
        return norm
    root = entry.root.rstrip("/")
    if norm == entry.root:
        remainder = ""
    else:
        remainder = norm[len(root):] if root else norm
    agent = (entry.mount_point.rstrip("/") + remainder) or "/"
    return agent


__all__ = [
    "MountEntry",
    "parse_mountinfo",
    "to_host",
    "from_host",
]
