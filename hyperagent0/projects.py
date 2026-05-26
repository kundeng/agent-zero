"""hyperagent0 project helpers (spec 09).

Thin layer on top of upstream's :mod:`python.helpers.projects` that
adds the spec 09 concepts:

* :func:`ensure_default_project` — make sure ``usr/projects/_default/``
  exists with a sensible ``project.json``. Called at daemon start so
  every later code path can rely on the implicit project existing.

* :func:`resolve_project_name` — given a context with no explicit
  project binding, returns ``"_default"`` rather than ``None``. The
  rest of the codebase can use this to collapse the historic
  ``if project_name:`` branches.

Imports stay light (no upstream imports at module load) so the helper
remains usable from cold-start paths.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


DEFAULT_PROJECT_NAME = "_default"


def _project_root() -> Path:
    """Return the absolute path to ``usr/projects/`` in the repo.

    Resolved off this module's location so it works in editable
    installs and the daemon's chdir context.
    """

    # hyperagent0/ is at repo root; usr/projects/ is sibling.
    return Path(__file__).resolve().parent.parent / "usr" / "projects"


def project_dir(name: str) -> Path:
    return _project_root() / name


def project_meta_dir(name: str) -> Path:
    return project_dir(name) / ".a0proj"


def ensure_default_project(workdir_path: Optional[str] = None) -> Path:
    """Create the ``_default`` project skeleton if it doesn't exist.

    The skeleton is the minimum a project needs to be usable:

    * ``project.json`` with title/description/instructions=""
    * empty ``instructions/`` dir
    * empty ``knowledge/`` dir
    * empty ``skills/`` dir

    ``secrets.env`` is intentionally NOT created so global
    ``usr/secrets.env`` applies. ``mcp_servers.json`` is NOT created
    so the global MCP config applies (per spec 10 D1).

    Optional ``workdir_path`` is recorded into ``project.json`` under
    the ``project_folder`` key so the sandbox / code-execution tool
    keeps using the operator's chosen working directory rather than
    the new project folder. Matches spec 09 D2 caveat ("if a user
    wants the sandbox/code_exec cwd to be the legacy global
    workdir_path, they set _default.project_folder = workdir_path").

    Idempotent: safe to call at every daemon start.
    Returns the project directory path.
    """

    pdir = project_dir(DEFAULT_PROJECT_NAME)
    meta = project_meta_dir(DEFAULT_PROJECT_NAME)
    project_json = meta / "project.json"

    if project_json.exists():
        return pdir

    logger.info(
        "ensure_default_project: bootstrapping %s", pdir
    )

    meta.mkdir(parents=True, exist_ok=True)
    (meta / "instructions").mkdir(exist_ok=True)
    (meta / "knowledge").mkdir(exist_ok=True)
    (meta / "skills").mkdir(exist_ok=True)

    payload: dict[str, Any] = {
        "title": "Default",
        "description": (
            "Implicit project for chats with no explicit binding. "
            "Created automatically; safe to edit."
        ),
        "instructions": "",
        "color": "#888888",
        "git_url": "",
    }
    if workdir_path:
        payload["project_folder"] = workdir_path

    project_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return pdir


def resolve_project_name(explicit_name: Optional[str]) -> str:
    """Resolve a project name; ``None`` / empty becomes :data:`DEFAULT_PROJECT_NAME`.

    Use this anywhere the upstream code branches on
    ``if project_name:`` and would otherwise need to keep two code
    paths. Collapsing the branches is the spec 09 D2 simplification.
    """

    if explicit_name and str(explicit_name).strip():
        return str(explicit_name).strip()
    return DEFAULT_PROJECT_NAME


# ---------------------------------------------------------------------------
# Spec 10 P1 — per-project capability helpers
# ---------------------------------------------------------------------------


def load_project_mcp_servers(name: str) -> Optional[str]:
    """Return the project's MCP servers JSON string, or ``None``.

    Spec 10 D1: an ``.a0proj/mcp_servers.json`` file at the project's
    metadata folder *replaces* the global ``settings.json.mcp_servers``
    for that project. We return the raw string (not parsed JSON) because
    the upstream :class:`MCPConfig.update` consumer expects a string.

    Returns ``None`` when the file is missing OR contains an empty /
    whitespace-only payload — that's the signal for the caller to fall
    back to the global config. Malformed JSON is propagated as the
    raw string so the upstream parser surfaces the error in its
    standard place rather than us guessing.
    """

    pj = project_meta_dir(name) / "mcp_servers.json"
    try:
        content = pj.read_text(encoding="utf-8")
    except OSError:
        return None
    if not content.strip():
        return None
    return content


def load_project_network_allow(name: str) -> list[str]:
    """Return the project's ``network.allow`` list from ``project.json``.

    Spec 10 D2: the sandbox network allowlist is layered — operator's
    global (``Settings.sandbox_network_default``) ∪ per-project
    (``project.json.network.allow``). This is the per-project side of
    the union; the global side is read by ``srt._global_network_default``.

    Returns ``[]`` when the file is missing, malformed, or doesn't
    declare a network section — defensive, since a broken project.json
    must not prevent the sandbox from coming up.
    """

    pj = project_meta_dir(name) / "project.json"
    try:
        with pj.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    net = data.get("network")
    if not isinstance(net, dict):
        return []
    allow = net.get("allow", [])
    if not isinstance(allow, list):
        return []
    return [str(host) for host in allow if isinstance(host, (str, bytes))]
