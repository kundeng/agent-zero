"""Graceful shutdown coordinator for the hyperagent0 daemon.

Task 1.6 from spec 03 calls for "on SIGTERM/SIGINT: pause all
AgentContexts, wait for in-flight tools, persist state, close
connections." We implement this here, called from the signal handler
installed by :mod:`hyperagent0.cli_commands.start`.

Rationale for placement
-----------------------
The CLAUDE.md convention says "extensions over core edits" — prefer
``python/extensions/<hook>/`` files to patching ``agent.py``. But the
existing extension hooks (``monologue_start``, ``tool_execute_after``,
etc.) all run inside the agent's own message loop. There is no
``daemon_shutdown`` or process-level lifecycle hook in upstream Agent
Zero. Adding one would itself require patching ``agent.py`` and the
extension machinery.

Instead, this module reaches into ``agent.AgentContext.all()`` from
outside the agent loop, sets ``paused = True`` on every live context,
waits for their in-flight async tasks to finish, and then asks the
upstream ``process`` helper to shut the uvicorn server down. Nothing
in ``agent.py`` needs to change; the existing public API
(``AgentContext.all()``, ``context.paused``, ``context.task``) is
sufficient.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any


def _ensure_repo_on_path() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _safe_print(msg: str) -> None:
    try:
        print(f"[hyperagent0] {msg}", flush=True)
    except Exception:  # pragma: no cover - stdout closed during shutdown
        pass


def graceful_shutdown(timeout: float = 25.0) -> None:
    """Pause all agents, wait for in-flight work, stop the server.

    Best-effort: every step is wrapped in a broad try/except so a
    failure in one phase doesn't block the rest of the shutdown. The
    daemon's lock release is the caller's responsibility (see
    :mod:`hyperagent0.cli_commands.start`).
    """

    _ensure_repo_on_path()

    deadline = time.monotonic() + timeout

    # Phase 1: pause every AgentContext so no new agent steps start.
    contexts: list[Any] = []
    try:
        import agent as agent_mod  # type: ignore

        contexts = list(agent_mod.AgentContext.all())
        for ctx in contexts:
            try:
                ctx.paused = True
            except Exception:  # pragma: no cover - defensive
                pass
        if contexts:
            _safe_print(f"paused {len(contexts)} agent context(s)")
    except Exception as exc:  # pragma: no cover - agent not importable
        _safe_print(f"could not enumerate agent contexts: {exc}")

    # Phase 2: wait for in-flight tool calls / monologues to finish.
    # ``ctx.task`` is a DeferredTask; we poll ``is_alive`` until either
    # everything stops or the deadline elapses.
    for ctx in contexts:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        task = getattr(ctx, "task", None)
        if task is None:
            continue
        is_alive = getattr(task, "is_alive", None)
        if not callable(is_alive):
            continue
        try:
            while is_alive() and (deadline - time.monotonic()) > 0:
                time.sleep(0.1)
        except Exception:  # pragma: no cover - defensive
            pass

    # Phase 2.5: stop chat channels (spec 04 task 1.6). Done before
    # the HTTP server stop so adapter long-poll loops have a chance to
    # cancel cleanly. Import is lazy so daemons without channels never
    # pay for it.
    try:
        from hyperagent0.channels.lifecycle import stop_all_channels  # type: ignore

        stop_all_channels(timeout=min(10.0, max(2.0, deadline - time.monotonic())))
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        _safe_print(f"could not stop channels cleanly: {exc}")

    # Phase 3: stop the HTTP server so uvicorn unwinds cleanly. The
    # existing helper in python/helpers/process.py is the upstream
    # hook for this.
    try:
        from python.helpers import process as process_helper  # type: ignore

        process_helper.stop_server()
        _safe_print("server stop requested")
    except Exception as exc:  # pragma: no cover - server may already be down
        _safe_print(f"could not stop server cleanly: {exc}")

    # Phase 4: kill any still-running agent tasks. We've already given
    # them up to ``timeout`` seconds to wind down; anything still alive
    # at this point is stuck.
    for ctx in contexts:
        task = getattr(ctx, "task", None)
        if task is None:
            continue
        kill = getattr(task, "kill", None)
        if callable(kill):
            try:
                kill()
            except Exception:  # pragma: no cover - defensive
                pass
