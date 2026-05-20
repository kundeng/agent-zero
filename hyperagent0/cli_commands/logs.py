"""``haz logs`` — read daemon log files.

Reads from ``~/.hyperagent0/logs/daemon.log`` (the path that
``haz start -d`` writes to). The implementation is stdlib-only so it
stays inside the cold-start budget for the common case of
``haz logs -f``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import click

from .. import daemon as _daemon


def _tail_lines(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines of ``path`` without loading the whole
    file. Falls back to ``readlines()`` for small files."""

    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return []
    if size == 0:
        return []
    block = 4096
    data = b""
    with path.open("rb") as f:
        end = size
        while end > 0 and data.count(b"\n") <= n:
            read = min(block, end)
            end -= read
            f.seek(end)
            data = f.read(read) + data
    lines = data.splitlines()[-n:]
    return [ln.decode("utf-8", errors="replace") for ln in lines]


def _parse_since(spec: str) -> float | None:
    """Parse a simple duration like ``5m``, ``2h``, ``30s``. Returns
    seconds, or ``None`` if the spec can't be parsed."""

    spec = spec.strip()
    if not spec:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if spec[-1] in units:
        try:
            return int(spec[:-1]) * units[spec[-1]]
        except ValueError:
            return None
    try:
        return float(spec)
    except ValueError:
        return None


@click.command("logs")
@click.option("-f", "--follow", is_flag=True, default=False, help="Stream new lines as they arrive.")
@click.option("-n", "--lines", "num_lines", type=int, default=100, show_default=True, help="Number of trailing lines to show first.")
@click.option("--since", "since", default=None, metavar="DURATION", help="Only show logs newer than DURATION (e.g. 5m, 2h, 30s).")
@click.option("--file", "log_path", default=None, metavar="PATH", help="Read this log file instead of the default daemon.log.")
def command(follow: bool, num_lines: int, since: str | None, log_path: str | None) -> None:
    """Show daemon log output."""

    path = Path(log_path) if log_path else _daemon.logs_dir() / "daemon.log"
    if not path.exists():
        click.echo(f"hyperagent0: no log file at {path}", err=True)
        raise click.exceptions.Exit(1)

    since_seconds = _parse_since(since) if since else None
    cutoff_mtime: float | None = None
    if since_seconds is not None:
        cutoff_mtime = time.time() - since_seconds

    lines = _tail_lines(path, num_lines)
    if cutoff_mtime is not None:
        # We don't have per-line timestamps in the generic case; this is
        # a best-effort filter against the file mtime.
        try:
            if path.stat().st_mtime < cutoff_mtime:
                lines = []
        except OSError:
            pass

    for line in lines:
        click.echo(line)

    if not follow:
        return

    # Simple tail -f loop. Re-open if the file is rotated.
    inode = path.stat().st_ino
    f = path.open("rb")
    f.seek(0, os.SEEK_END)
    try:
        while True:
            chunk = f.readline()
            if chunk:
                click.echo(chunk.decode("utf-8", errors="replace").rstrip("\n"))
                continue
            # No new data; sleep briefly and check for rotation.
            time.sleep(0.5)
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if st.st_ino != inode:
                try:
                    f.close()
                except OSError:
                    pass
                f = path.open("rb")
                inode = st.st_ino
    except KeyboardInterrupt:
        pass
    finally:
        try:
            f.close()
        except OSError:
            pass
