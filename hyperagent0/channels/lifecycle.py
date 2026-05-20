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
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)


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


def _instantiate_adapter(name: str, cfg) -> Any:
    """Import the adapter module and construct the adapter instance.

    Importing the module triggers ``register_channel(...)``, populating
    the registry in :mod:`hyperagent0.channels.base`.
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
    return cls(cfg.raw if hasattr(cfg, "raw") else cfg)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_enabled_channels() -> None:
    """Boot every channel whose config has ``enabled: true``.

    Called by :mod:`hyperagent0.cli_commands.start` after the upstream
    server is up. Safe to call more than once — re-entry is a no-op.
    """

    global _loop, _thread, _router, _started

    with _lock:
        if _started:
            return
        try:
            from .config import load_channels_config
            from .router import ChannelRouter
        except Exception:
            logger.exception("could not import channels stack")
            return

        configs = load_channels_config()
        enabled = {n: c for n, c in configs.items() if c.enabled}
        if not enabled:
            logger.info("no channels enabled; channels subsystem idle")
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

        _router = ChannelRouter(channel_configs=enabled)

        # Connect each enabled adapter on the channels loop.
        for name, cfg in enabled.items():
            try:
                adapter = _instantiate_adapter(name, cfg)
            except Exception:
                logger.exception(
                    "could not instantiate channel adapter %s; skipping", name
                )
                continue
            _router.register(adapter, cfg)
            _channels[name] = adapter
            fut = asyncio.run_coroutine_threadsafe(adapter.connect(), _loop)
            try:
                fut.result(timeout=30)
                logger.info("channel %s started", name)
            except Exception:
                logger.exception(
                    "channel %s connect failed; leaving it offline", name
                )

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
        for name, adapter in list(_channels.items()):
            try:
                fut = asyncio.run_coroutine_threadsafe(adapter.disconnect(), _loop)
                fut.result(timeout=timeout)
                logger.info("channel %s stopped", name)
            except Exception:
                logger.exception("channel %s disconnect failed", name)

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
