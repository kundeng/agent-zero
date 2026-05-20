"""Resolve per-project sandbox mode (spec 01-host-first task 1.6).

Per spec D5, ``sandbox_mode`` lives in two places:

- Global default in ``settings.json`` (Settings.sandbox_mode, task 1.3).
- Per-project override in ``project.json#sandbox.mode`` (BasicProjectData
  extension, task 1.6).

Resolution rule: ``project.sandbox.mode == "inherit"`` → use global; any
other value wins.

We keep this helper outside ``python/helpers/projects.py`` so the upstream
patch there stays small (just the TypedDict extension). The AgentConfig
plumbing — storing the resolved mode where the code execution tool can read
it — uses ``AgentConfig.additional`` to avoid widening the agent.py file
contract.
"""

from __future__ import annotations

from typing import Any

#: Key into AgentConfig.additional where the resolved sandbox mode lives.
AGENT_CONFIG_KEY_SANDBOX_MODE = "sandbox_mode"


def resolve_sandbox_mode(
    global_settings: dict[str, Any], project_name: str | None
) -> str:
    """Resolve the active sandbox mode for the given (global, project) pair.

    Falls back to the global default when:
    - no project is active, or
    - the project has no ``sandbox`` block, or
    - the project's mode is ``"inherit"``.

    Returns the resolved mode string (e.g., ``"none"``, ``"sandbox"``, ``"ssh"``).
    Spec 05 will broaden the literal set with ``docker``/``podman``/``cgroup``.
    """
    global_mode = str(global_settings.get("sandbox_mode") or "none")

    if not project_name:
        return global_mode

    # Lazy import — this module is imported during AgentContext setup and
    # we don't want to drag persist_chat/git/file_tree along for the ride
    # unless we actually have a project to resolve.
    try:
        from python.helpers import projects as _projects
    except Exception:
        return global_mode

    try:
        data = _projects.load_basic_project_data(project_name)
    except Exception:
        return global_mode

    block = data.get("sandbox") or {}
    project_mode = str(block.get("mode") or "inherit")
    if project_mode in ("", "inherit"):
        return global_mode
    return project_mode


def set_agent_sandbox_mode(config: Any, mode: str) -> None:
    """Store the resolved mode on an AgentConfig via .additional[]."""
    if not hasattr(config, "additional") or config.additional is None:
        config.additional = {}
    config.additional[AGENT_CONFIG_KEY_SANDBOX_MODE] = mode


def get_agent_sandbox_mode(config: Any, default: str = "") -> str:
    """Read the resolved sandbox mode previously set on an AgentConfig.

    Returns ``default`` (typically ``""``) if the field is unset, which
    lets call sites fall back to legacy ``code_exec_ssh_enabled`` logic
    during the transition window.
    """
    additional = getattr(config, "additional", None) or {}
    return str(additional.get(AGENT_CONFIG_KEY_SANDBOX_MODE) or default)
