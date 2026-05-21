"""Numbered SQL migration runner for the channels DB (spec 06 task 1.5).

The migrator looks at the sibling directory's ``NNN_*.sql`` files, sorts
them by their numeric prefix, and applies each one that's not already
recorded in the ``schema_migrations`` table.

Migrations are applied inside a single ``BEGIN ... COMMIT`` per file so
a crash mid-application leaves a clean state. ``IF NOT EXISTS`` clauses
in 001 keep first-boot upgrades from existing spec-04 installs idempotent.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


_VERSION_RE = re.compile(r"^(\d{3})_.+\.sql$")


def _migration_dir() -> Path:
    return Path(__file__).resolve().parent


def _discover_migrations(directory: Path | None = None) -> list[tuple[int, Path]]:
    """Return ``[(version, path), ...]`` sorted by version ascending."""

    out: list[tuple[int, Path]] = []
    for entry in (directory or _migration_dir()).iterdir():
        if not entry.is_file():
            continue
        m = _VERSION_RE.match(entry.name)
        if not m:
            continue
        out.append((int(m.group(1)), entry))
    out.sort(key=lambda pair: pair[0])
    return out


class Migrator:
    """Applies pending migrations against a SQLite connection."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lock: threading.RLock | None = None,
        directory: Path | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock or threading.RLock()
        self._dir = directory

    def upgrade(self) -> list[int]:
        """Apply every migration newer than the highest applied version.

        Returns the list of versions actually applied this call.
        """

        applied: list[int] = []
        with self._lock:
            self._ensure_table()
            current = self._max_version()
            for version, path in _discover_migrations(self._dir):
                if version <= current:
                    continue
                sql = path.read_text(encoding="utf-8")
                logger.info(
                    "applying channels migration %03d (%s)", version, path.name
                )
                self._apply(version, sql)
                applied.append(version)
        return applied

    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def _max_version(self) -> int:
        cur = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def _apply(self, version: int, sql: str) -> None:
        try:
            self._conn.execute("BEGIN")
            self._conn.executescript(sql)
            self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, time.time()),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
