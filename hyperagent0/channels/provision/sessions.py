"""In-memory session cache for multi-step provisioning flows (spec 08 D5).

Provisioning is inherently multi-step: Slack's wizard, for example,
needs the ``app_id`` and ``client_secret`` minted in step 1 to be
available to the OAuth callback handler in step 2. Threading those
values through the HTTP boundary as opaque blobs would either expose
them to the browser or force the UI to re-send everything on each
step — both are worse than keeping a small in-memory cache on the
daemon side.

The cache is intentionally minimal:

* Keyed by a UUID-shaped session id minted at wizard start.
* TTL is 30 minutes — long enough for the slowest provisioning flow
  (Slack with paste fallbacks), short enough that a forgotten browser
  tab does not pin scratch space indefinitely.
* In-process only. A daemon restart in the middle of a provisioning
  session means the user starts over. That's an acceptable trade-off
  given the alternative — persisting half-provisioned state — is
  strictly worse for security.

The cache is shared across all provisioners. Each session is scoped
to a single ``channel_type``; cross-platform state mixing is a
programming error (and the helper here enforces it).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


_DEFAULT_TTL_S = 30 * 60  # 30 minutes (spec 08 D5)


@dataclass
class _Session:
    """One in-flight provisioning session.

    ``scratch`` is the dict provisioners stuff with intermediate
    values. The framework writes nothing into it directly.
    """

    session_id: str
    channel_type: str
    created_at: float
    last_used_at: float
    scratch: dict[str, Any] = field(default_factory=dict)


class SessionCache:
    """Thread-safe TTL cache for provisioning sessions.

    Designed for a small population (a handful of in-flight sessions
    per daemon — typically zero or one). Lookups O(1); eviction is
    amortized on every access via :meth:`_evict_expired`.
    """

    def __init__(self, ttl_s: int = _DEFAULT_TTL_S) -> None:
        self._lock = threading.RLock()
        self._sessions: Dict[str, _Session] = {}
        self._ttl_s = ttl_s

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, channel_type: str) -> _Session:
        """Mint a fresh session for ``channel_type`` and return it."""

        with self._lock:
            self._evict_expired()
            now = time.time()
            session = _Session(
                session_id=uuid.uuid4().hex,
                channel_type=channel_type,
                created_at=now,
                last_used_at=now,
            )
            self._sessions[session.session_id] = session
            return session

    def get(self, session_id: str, channel_type: str) -> Optional[_Session]:
        """Return the session iff it exists, hasn't expired, and matches type.

        Cross-platform mismatch returns ``None`` — defensive against a
        confused client posting a Slack session id to a Telegram
        endpoint.
        """

        with self._lock:
            self._evict_expired()
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.channel_type != channel_type:
                return None
            session.last_used_at = time.time()
            return session

    def get_or_start(self, channel_type: str, session_id: Optional[str]) -> _Session:
        """Return the existing session, or mint a new one.

        Convenience used by the Flask handlers: if the client posted a
        session id and it's valid, reuse it; otherwise start fresh.
        Mismatched ``channel_type`` falls back to fresh as well.
        """

        if session_id:
            existing = self.get(session_id, channel_type)
            if existing is not None:
                return existing
        return self.start(channel_type)

    def end(self, session_id: str) -> None:
        """Drop a session at the end of a successful flow.

        Optional — the TTL handles abandoned sessions on its own.
        """

        with self._lock:
            self._sessions.pop(session_id, None)

    # ------------------------------------------------------------------
    # Test seams
    # ------------------------------------------------------------------

    def size(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _evict_expired(self) -> None:
        # Caller must hold the lock.
        now = time.time()
        cutoff = now - self._ttl_s
        expired = [
            sid for sid, s in self._sessions.items() if s.last_used_at < cutoff
        ]
        for sid in expired:
            del self._sessions[sid]


# ---------------------------------------------------------------------------
# Module-level singleton (used by the Flask handlers)
# ---------------------------------------------------------------------------


_default_cache: Optional[SessionCache] = None
_default_cache_lock = threading.Lock()


def default_cache() -> SessionCache:
    """Return the process-wide cache used by the Flask handlers.

    Tests that need isolation construct their own :class:`SessionCache`
    rather than touching this one — the default lives only so the
    HTTP handlers don't have to thread an instance through every
    call.
    """

    global _default_cache
    if _default_cache is None:
        with _default_cache_lock:
            if _default_cache is None:
                _default_cache = SessionCache()
    return _default_cache
