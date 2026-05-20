"""Daemon lifecycle helpers for hyperagent0.

This module owns the singleton-enforcement surface for the daemon
(:class:`fcntl.flock` on a dedicated lock file) and the PID file used
for ``haz stop`` / ``haz status``. Both files live under
``~/.hyperagent0/`` (D2, D3).

The helpers here are deliberately tiny and free of third-party imports
so that ``haz status`` and ``haz stop`` — which call into this module —
stay within the cold-start budget (D5).
"""

from __future__ import annotations

import errno
import fcntl
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def state_dir() -> Path:
    """Return ``~/.hyperagent0/``, creating it on first use."""

    d = Path(os.path.expanduser("~/.hyperagent0"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    """Return ``~/.hyperagent0/logs/``, creating it on first use."""

    d = state_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file() -> Path:
    return state_dir() / "hyperagent0.pid"


def lock_file() -> Path:
    return state_dir() / "hyperagent0.lock"


def sock_file() -> Path:
    return state_dir() / "daemon.sock"


# ---------------------------------------------------------------------------
# PID / lock primitives
# ---------------------------------------------------------------------------


@dataclass
class LockHandle:
    """Opaque handle returned by :func:`acquire_lock`.

    Callers should hold this for the lifetime of the daemon process and
    pass it to :func:`release_lock` on shutdown. The underlying file
    descriptor is kept open intentionally — closing it would release the
    advisory ``flock`` and let a second daemon start.
    """

    fd: int
    path: Path


def acquire_lock() -> Optional[LockHandle]:
    """Try to acquire the singleton daemon lock.

    Returns a :class:`LockHandle` on success, or ``None`` if another
    process already holds the lock (i.e. the daemon is already running).
    The PID file is written as a side effect of a successful acquisition.
    """

    path = lock_file()
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return None
        raise
    # Write our PID to the conventional pid file so external tools
    # (systemd, monitoring scripts, ``haz stop``) can find us.
    try:
        pid_file().write_text(f"{os.getpid()}\n")
    except OSError:
        # Lock acquired but PID file unwritable — release and abort.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        raise
    return LockHandle(fd=fd, path=path)


def release_lock(handle: Optional[LockHandle]) -> None:
    """Release a lock previously returned by :func:`acquire_lock`."""

    # Best-effort PID file cleanup. Ignore errors so shutdown stays
    # robust even on read-only filesystems.
    try:
        pid_file().unlink(missing_ok=True)
    except OSError:
        pass
    if handle is None:
        return
    try:
        fcntl.flock(handle.fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(handle.fd)
    except OSError:
        pass


def get_pid() -> Optional[int]:
    """Return the PID recorded in the pid file, or ``None``."""

    try:
        raw = pid_file().read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw.split()[0])
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal — count it as
        # alive (the daemon is still occupying our slot).
        return True
    except OSError:
        return False
    return True


def is_running() -> bool:
    """True if a daemon appears to be running.

    The check is conservative: we look at the PID file, verify the
    process exists, and additionally try a non-blocking lock acquisition
    to detect orphaned PID files. Either signal counts as 'running'.
    """

    pid = get_pid()
    if pid is not None and _pid_alive(pid):
        return True

    # No live PID; double-check via the advisory lock in case the PID
    # file went missing but the daemon is still up.
    path = lock_file()
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        # We got the lock — nothing was running. Release immediately.
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def send_signal(sig: int = signal.SIGTERM) -> bool:
    """Send ``sig`` to the daemon. Return ``True`` if delivered."""

    pid = get_pid()
    if pid is None or not _pid_alive(pid):
        return False
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def wait_for_exit(timeout: float = 30.0, poll: float = 0.2) -> bool:
    """Block up to ``timeout`` seconds for the daemon process to exit."""

    pid = get_pid()
    if pid is None:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(poll)
    return not _pid_alive(pid)
