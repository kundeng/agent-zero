"""Daemon-side lifecycle helpers for channel adapters (spec 04, task 1.6).

The :mod:`hyperagent0.cli_commands.start` foreground server is a
synchronous Flask/uvicorn process. Channel adapters live on an
``asyncio`` event loop instead. We bridge the two by running the
adapters on a dedicated background thread that owns its own loop. The
daemon's main thread keeps serving HTTP; on SIGTERM,
:func:`hyperagent0.shutdown.graceful_shutdown` calls
:func:`stop_all_channels` which schedules ``disconnect()`` for every
adapter on that loop and then tears the loop down.

Module-level state is intentional: there is exactly one daemon process
per host (singleton lock in :mod:`hyperagent0.daemon`), so a single
process-global state vector is the simplest reliable representation.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Network-error detection (spec 06 D4)
# ---------------------------------------------------------------------------

# Backoff schedule for retry-on-NetworkError around adapter connect().
# Matches NanoClaw's pattern from src/channels/channel-registry.ts:11.
_CONNECT_RETRY_DELAYS_S: tuple[float, ...] = (2.0, 5.0, 10.0)


def is_network_error(exc: BaseException) -> bool:
    """Return True iff ``exc`` looks like a transient network failure.

    Used by :func:`start_enabled_channels` to retry adapter ``connect()``
    on network errors only — misconfigs (bad token, missing intent)
    still fail fast on the first attempt.

    Detection covers stdlib network exceptions plus per-SDK network
    exception types when their packages are installed. We do NOT import
    the SDKs at module top — each SDK's check is gated by a try/except
    so non-installed channels don't break the helper.
    """

    # Stdlib transients — fast path, covers ConnectionResetError,
    # ConnectionRefusedError, ConnectionAbortedError, TimeoutError,
    # asyncio.TimeoutError, socket.gaierror.
    if isinstance(
        exc,
        (
            ConnectionError,
            TimeoutError,
            asyncio.TimeoutError,
            socket.gaierror,
        ),
    ):
        return True

    # Telegram
    try:
        from telegram.error import NetworkError as _TgNetErr, TimedOut as _TgTimeout  # type: ignore

        if isinstance(exc, (_TgNetErr, _TgTimeout)):
            return True
    except Exception:
        pass

    # Slack
    try:
        from slack_sdk.errors import SlackApiError  # type: ignore

        if isinstance(exc, SlackApiError):
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None) if resp is not None else None
            if status in (429, 502, 503, 504):
                return True
    except Exception:
        pass

    # Discord
    try:
        import discord  # type: ignore

        if isinstance(exc, getattr(discord, "ConnectionClosed", tuple())):
            return True
        http_exc = getattr(discord, "HTTPException", None)
        if http_exc is not None and isinstance(exc, http_exc):
            status = getattr(exc, "status", None)
            if status in (429, 500, 502, 503, 504):
                return True
    except Exception:
        pass

    return False


# ---------------------------------------------------------------------------
# Process-global state
# ---------------------------------------------------------------------------

# Holding refs:
#   _loop          → asyncio loop running on the channels thread
#   _thread        → the channels thread itself
#   _router        → ChannelRouter instance (singleton per daemon)
#   _channels      → name → BaseChannel for every connected adapter
_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_router: Any = None
_channels: dict[str, Any] = {}
_started = False
_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Adapter discovery
# ---------------------------------------------------------------------------


def _adapter_module(name: str) -> Optional[str]:
    """Map a channel name to its adapter module path.

    Returns the import path or ``None`` if the channel is not known.
    Keeping this lookup explicit (rather than scanning the package)
    means an SDK never gets imported by mistake.
    """

    return {
        "telegram": "hyperagent0.channels.telegram",
        "slack": "hyperagent0.channels.slack",
        "discord": "hyperagent0.channels.discord",
    }.get(name)


def _instantiate_adapter(name: str, cfg, *, bot_name: str = "_legacy") -> Any:
    """Import the adapter module and construct the adapter instance.

    Importing the module triggers ``register_channel(...)``, populating
    the registry in :mod:`hyperagent0.channels.base`. ``bot_name``
    propagates the spec-09 D5 bot identity into the adapter so
    InboundMessages stamp the right routing key.
    """

    import importlib

    from .base import get_channel_class

    mod_path = _adapter_module(name)
    if mod_path is None:
        raise RuntimeError(f"unknown channel adapter: {name}")
    importlib.import_module(mod_path)

    cls = get_channel_class(name)
    if cls is None:
        raise RuntimeError(
            f"channel adapter module {mod_path!r} did not register class {name!r}"
        )
    raw = cfg.raw if hasattr(cfg, "raw") else cfg
    return cls(raw, bot_name=bot_name)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_enabled_channels() -> None:
    """Boot every bot whose config has ``enabled: true``.

    Spec 09 D5: a platform's value in ``channels.json`` is a list of
    bot configs. We instantiate one adapter per enabled bot, register
    each under ``(channel_type, bot_name)``, and treat the
    process-global ``_channels`` map the same way.

    Called by :mod:`hyperagent0.cli_commands.start` after the upstream
    server is up. Safe to call more than once — re-entry is a no-op.
    """

    global _loop, _thread, _router, _started

    with _lock:
        if _started:
            return
        try:
            from .config import load_bot_configs
            from .router import ChannelRouter
        except Exception:
            logger.exception("could not import channels stack")
            return

        try:
            from hyperagent0.projects import ensure_default_project

            ensure_default_project()
        except Exception:
            # Project bootstrap failure must not block channels from coming up
            # — log and proceed; per-chat project activation falls back to
            # whatever upstream's resolve_project_name yields.
            logger.exception("ensure_default_project failed (non-fatal)")

        bots_by_platform = load_bot_configs()
        enabled_bots: list = []  # flat [(channel_type, BotConfig), ...]
        for channel_type, bots in bots_by_platform.items():
            for bot in bots:
                if bot.enabled:
                    enabled_bots.append((channel_type, bot))
        if not enabled_bots:
            logger.info("no bots enabled; channels subsystem idle")
            _started = True
            return

        # Spin up a dedicated loop thread.
        loop_ready = threading.Event()

        def _runner() -> None:
            global _loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _loop = loop
            loop_ready.set()
            try:
                loop.run_forever()
            finally:
                # Drain remaining tasks before closing.
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:  # pragma: no cover - defensive
                    pass
                loop.close()

        _thread = threading.Thread(
            target=_runner, name="hyperagent0-channels", daemon=True
        )
        _thread.start()
        loop_ready.wait(timeout=5.0)
        if _loop is None:  # pragma: no cover - thread didn't init
            logger.error("channels loop failed to initialize")
            return

        _router = ChannelRouter()

        # Connect each enabled bot's adapter on the channels loop.
        for channel_type, bot in enabled_bots:
            label = f"{channel_type}/{bot.bot_name}"
            try:
                adapter = _instantiate_adapter(
                    channel_type, bot, bot_name=bot.bot_name
                )
            except Exception:
                logger.exception(
                    "could not instantiate channel adapter %s; skipping", label
                )
                continue
            _router.register(adapter, bot)
            _channels[(channel_type, bot.bot_name)] = adapter
            if not _connect_with_retry(adapter, label):
                # Already logged inside the helper. Leave adapter offline;
                # the daemon keeps running so other bots can come up.
                continue
            logger.info("channel %s started", label)

        _started = True


def stop_all_channels(timeout: float = 10.0) -> None:
    """Disconnect every live adapter and shut the channels loop down.

    Called from :func:`hyperagent0.shutdown.graceful_shutdown`. Wraps
    every step in a broad ``try/except`` so a misbehaving adapter
    cannot block the daemon's SIGTERM path.
    """

    global _loop, _thread, _router, _channels, _started

    with _lock:
        if not _started:
            return
        if _loop is None or not _channels:
            _started = False
            return

        # Schedule disconnects on the channels loop and wait for each.
        for key, adapter in list(_channels.items()):
            label = f"{key[0]}/{key[1]}" if isinstance(key, tuple) else str(key)
            try:
                fut = asyncio.run_coroutine_threadsafe(adapter.disconnect(), _loop)
                fut.result(timeout=timeout)
                logger.info("channel %s stopped", label)
            except Exception:
                logger.exception("channel %s disconnect failed", label)

        # Stop the loop.
        try:
            _loop.call_soon_threadsafe(_loop.stop)
        except Exception:
            logger.exception("could not stop channels loop cleanly")

        # Wait for the thread to wind down.
        if _thread is not None:
            _thread.join(timeout=timeout)

        _channels.clear()
        _router = None
        _loop = None
        _thread = None
        _started = False


def get_router() -> Any:
    """Return the live :class:`ChannelRouter`, if any.

    Exposed so a future "proactive send from agent code" implementation
    (open question 3 in the spec) has a single entry point.
    """

    return _router


def running_adapters() -> dict[str, dict[str, Any]]:
    """Snapshot live-adapter state for the status API (spec 08 1.7).

    Returns a mapping of ``"channel_type"`` or ``"channel_type/bot_name"``
    → ``{"live": bool}``. Spec 09 added the bot-name suffix for
    multi-bot installs; single-bot (``_legacy``) entries keep the bare
    ``channel_type`` key so existing status consumers don't break.

    Future expansion ground: ``last_error`` once adapters surface a
    last-failure timestamp, ``connected_at`` once we record it.
    """

    with _lock:
        snapshot: dict[str, dict[str, Any]] = {}
        for key, adapter in _channels.items():
            if isinstance(key, tuple):
                channel_type, bot_name = key
                label = (
                    channel_type
                    if bot_name == "_legacy"
                    else f"{channel_type}/{bot_name}"
                )
            else:
                label = str(key)
            snapshot[label] = {
                "live": adapter is not None,
            }
        return snapshot


def restart_channels(timeout: float = 10.0) -> None:
    """Stop every live adapter and (re)boot all enabled channels.

    Called by ``/channels_apply`` after a provisioner writes new
    secrets / channels.json blocks. Idempotent — calling on a daemon
    with no live channels just runs :func:`start_enabled_channels`.

    The two halves run inside the module-level ``_lock`` (an
    :class:`threading.RLock` — re-entry is safe) so a parallel
    ``/channels_apply`` can't observe a half-restarted state.
    """

    with _lock:
        stop_all_channels(timeout=timeout)
        start_enabled_channels()


# ---------------------------------------------------------------------------
# Internal — retry helper (spec 06 D4)
# ---------------------------------------------------------------------------


def _connect_with_retry(adapter: Any, name: str) -> bool:
    """Call ``adapter.connect()`` on the channels loop with retry.

    Returns True on success, False if the adapter is still offline after
    exhausting the retry schedule. Only retries on network failures
    (see :func:`is_network_error`) — misconfigs fail fast.
    """

    if _loop is None:  # pragma: no cover - defensive
        return False

    schedule = (0.0,) + _CONNECT_RETRY_DELAYS_S  # first attempt has no delay
    last_exc: Optional[BaseException] = None
    for attempt, delay in enumerate(schedule):
        if delay > 0:
            # Sleep on the lifecycle thread, not the channels loop —
            # this is sync code in the daemon's startup path.
            import time as _time

            logger.warning(
                "channel %s connect failed (attempt %d) with network error; "
                "retrying in %.1fs",
                name,
                attempt,
                delay,
            )
            _time.sleep(delay)

        fut = asyncio.run_coroutine_threadsafe(adapter.connect(), _loop)
        try:
            fut.result(timeout=30)
            return True
        except Exception as exc:
            last_exc = exc
            if not is_network_error(exc):
                logger.exception(
                    "channel %s connect failed; leaving it offline (not retryable)",
                    name,
                )
                return False
            # Otherwise loop to the next delay.

    logger.error(
        "channel %s connect failed after %d retries; leaving it offline: %s",
        name,
        len(_CONNECT_RETRY_DELAYS_S),
        last_exc,
    )
    return False
