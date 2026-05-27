"""Per-project MCP resolver (spec 10 P1.2).

Upstream's :class:`python.helpers.mcp_handler.MCPConfig` is a process-
global singleton: :meth:`MCPConfig.get_instance` always returns the
same object, and :meth:`MCPConfig.update` mutates it in place. That's
fine when MCP is a single global tool set, but spec 10 D1 makes MCP
*per-project* (replace semantics, not overlay).

This module is the resolver that the runtime consumers use instead of
calling ``MCPConfig.get_instance()`` directly. The contract:

* If the agent's active project has ``.a0proj/mcp_servers.json`` with a
  non-empty payload, return a project-specific :class:`MCPConfig`
  instance (cached on first build).
* Otherwise, return the upstream global singleton — preserving the
  pre-spec-10 behavior for installs that never touch per-project MCP.

The cache is keyed on project name. Constructing an ``MCPConfig`` runs
``asyncio.run(_init_all())`` in its ``__init__``, which can't be
called from inside an existing event loop — so we build the instance
in a short-lived worker thread. After the first build the cached
instance is reused with zero overhead.

Caveats:

* No hot-swap. Changing ``mcp_servers.json`` requires restarting the
  daemon (matches the global behavior — global MCP also only refreshes
  on settings save → ``MCPConfig.update``). Hot-swap is P2.
* Project-specific MCPConfig instances keep their subprocesses /
  remote connections alive for the daemon lifetime. If a user creates
  100 projects with MCP, that's 100 server pools.
* Admin APIs (``python/api/mcp_*.py``) deliberately keep using
  ``MCPConfig.get_instance()`` — they show the *operator's* global
  MCP view in the settings UI, not whatever the currently active
  chat happens to be using.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from hyperagent0.projects import (
    load_project_mcp_servers,
    resolve_project_name,
)


logger = logging.getLogger(__name__)


_CACHE_LOCK = threading.Lock()
_PROJECT_CACHE: Dict[str, Any] = {}  # project_name → MCPConfig


def _build_project_mcp_config(servers_config: str) -> Any:
    """Construct an isolated :class:`MCPConfig` for one project.

    Runs the construction in a worker thread because ``MCPConfig.__init__``
    calls ``asyncio.run(_init_all())`` to fetch each server's tool list
    — and ``asyncio.run`` raises if a loop is already running on the
    calling thread (system-prompt rendering is async).
    """

    from python.helpers.mcp_handler import MCPConfig
    from python.helpers import dirty_json

    result: dict[str, Any] = {}

    def _run() -> None:
        try:
            parsed = dirty_json.try_parse(servers_config)
            servers_data = MCPConfig.normalize_config(parsed)
            if not isinstance(servers_data, list):
                servers_data = []
            # Filter out non-dict items the way MCPConfig.update does.
            servers_data = [s for s in servers_data if isinstance(s, dict)]
            result["instance"] = MCPConfig(servers_list=servers_data)
        except Exception as exc:  # noqa: BLE001 — surface to caller
            result["error"] = exc

    worker = threading.Thread(
        target=_run, name="mcp-project-init", daemon=True
    )
    worker.start()
    worker.join()

    if "error" in result:
        raise result["error"]
    return result["instance"]


def get_mcp_config_for_agent(agent: Any) -> Any:
    """Return the :class:`MCPConfig` that this agent should consult.

    Resolution order (spec 10 D1):

    1. Agent's active project (via ``agent.context.get_data("project")``,
       resolved through ``_default``).
    2. If that project has ``.a0proj/mcp_servers.json`` with a non-empty
       payload, return its cached per-project ``MCPConfig``.
    3. Otherwise, return the global ``MCPConfig.get_instance()``.

    Any unexpected failure during step 2 logs and falls back to the
    global instance — a broken per-project file must not take MCP down
    for everyone else in the daemon.
    """

    from python.helpers.mcp_handler import MCPConfig
    from python.helpers.projects import CONTEXT_DATA_KEY_PROJECT

    global_instance = MCPConfig.get_instance()

    try:
        project_name = resolve_project_name(
            agent.context.get_data(CONTEXT_DATA_KEY_PROJECT)
        )
    except Exception as exc:  # noqa: BLE001 — context shape can vary in tests
        logger.debug("mcp: could not resolve project from agent: %s", exc)
        return global_instance

    payload = load_project_mcp_servers(project_name)
    if not payload:
        return global_instance

    with _CACHE_LOCK:
        cached = _PROJECT_CACHE.get(project_name)
        if cached is not None:
            return cached

        try:
            instance = _build_project_mcp_config(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "mcp: failed to build per-project MCPConfig for %r, "
                "falling back to global: %s",
                project_name,
                exc,
            )
            return global_instance

        _PROJECT_CACHE[project_name] = instance
        return instance


def reset_cache(project_name: Optional[str] = None) -> None:
    """Drop cached per-project MCPConfig(s).

    ``project_name=None`` clears everything; otherwise just that
    project. The next ``get_mcp_config_for_agent`` call for that
    project will rebuild. Intended for test teardown and (eventually)
    a Web UI "reload MCP" action — not currently called anywhere in
    the runtime.
    """

    with _CACHE_LOCK:
        if project_name is None:
            _PROJECT_CACHE.clear()
        else:
            _PROJECT_CACHE.pop(project_name, None)
