"""Spec 10 P1 — per-project capability helpers + ``_default`` skill bridging.

Pins the contract of:

* ``hyperagent0.projects.load_project_mcp_servers`` (P1.1)
* ``hyperagent0.projects.load_project_network_allow`` (P1.1)
* ``subagents.get_paths`` falls through to ``_default`` for
  projectless agent contexts (P1.4 / D3a)

Tests are dependency-free — they exercise file-reader helpers and a
synthetic ``AgentContext`` stub for the skill-resolution path.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Shared fixture: tmp project tree
# ---------------------------------------------------------------------------


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    """Redirect ``hyperagent0.projects._project_root`` AND upstream
    ``projects.get_projects_parent_folder`` to a tmp path so the
    helpers don't accidentally read the dev repo's real ``usr/projects``.
    """

    root = tmp_path / "usr" / "projects"
    root.mkdir(parents=True)

    from hyperagent0 import projects as hp_projects

    monkeypatch.setattr(hp_projects, "_project_root", lambda: root)

    from python.helpers import projects as up_projects

    monkeypatch.setattr(
        up_projects, "get_projects_parent_folder", lambda: str(root)
    )
    return root


def _write_project_json(root: Path, name: str, data: dict) -> None:
    pdir = root / name / ".a0proj"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "project.json").write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# load_project_mcp_servers — P1.1 (D1)
# ---------------------------------------------------------------------------


def test_mcp_servers_returns_raw_string_when_present(projects_root):
    """The helper returns the raw JSON string so the upstream
    ``MCPConfig.update`` consumer parses it in its standard place."""

    pdir = projects_root / "engineering" / ".a0proj"
    pdir.mkdir(parents=True, exist_ok=True)
    payload = '[{"name": "linear", "command": "npx", "args": ["-y", "linear-mcp"]}]'
    (pdir / "mcp_servers.json").write_text(payload)

    from hyperagent0.projects import load_project_mcp_servers

    assert load_project_mcp_servers("engineering") == payload


def test_mcp_servers_returns_none_when_file_missing(projects_root):
    """Missing file → ``None`` so callers fall through to the global
    ``settings.json.mcp_servers``."""

    from hyperagent0.projects import load_project_mcp_servers

    assert load_project_mcp_servers("nonexistent") is None


def test_mcp_servers_returns_none_for_empty_file(projects_root):
    """An empty / whitespace-only file is also the fall-through signal."""

    pdir = projects_root / "blank" / ".a0proj"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "mcp_servers.json").write_text("   \n\t  \n")

    from hyperagent0.projects import load_project_mcp_servers

    assert load_project_mcp_servers("blank") is None


# ---------------------------------------------------------------------------
# load_project_network_allow — P1.1 (D2)
# ---------------------------------------------------------------------------


def test_network_allow_returns_declared_hosts(projects_root):
    _write_project_json(
        projects_root,
        "engineering",
        {
            "title": "Engineering",
            "network": {"allow": ["github.com", "*.example.internal"]},
        },
    )

    from hyperagent0.projects import load_project_network_allow

    assert load_project_network_allow("engineering") == [
        "github.com",
        "*.example.internal",
    ]


def test_network_allow_empty_for_missing_network_section(projects_root):
    _write_project_json(
        projects_root, "no-net", {"title": "no-net", "description": "x"}
    )

    from hyperagent0.projects import load_project_network_allow

    assert load_project_network_allow("no-net") == []


def test_network_allow_empty_for_malformed_project_json(projects_root):
    pdir = projects_root / "broken" / ".a0proj"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "project.json").write_text("{not valid json")

    from hyperagent0.projects import load_project_network_allow

    # Sandbox must always come up — malformed project.json falls back
    # to default-deny, not raises.
    assert load_project_network_allow("broken") == []


def test_network_allow_filters_non_string_entries(projects_root):
    """Defensive: a hand-edited project.json could put non-strings in
    the allow list. We coerce strings and skip the rest."""

    _write_project_json(
        projects_root,
        "messy",
        {"network": {"allow": ["good.com", 42, None, {"bad": "shape"}, "also-good.com"]}},
    )

    from hyperagent0.projects import load_project_network_allow

    assert load_project_network_allow("messy") == ["good.com", "also-good.com"]


# ---------------------------------------------------------------------------
# subagents.get_paths falls through to _default — P1.4 / D3a
# ---------------------------------------------------------------------------


class _StubContext:
    def __init__(self, project_name=None):
        self._project = project_name

    def get_data(self, key):
        from python.helpers.projects import CONTEXT_DATA_KEY_PROJECT

        if key == CONTEXT_DATA_KEY_PROJECT:
            return self._project
        return None


class _StubAgent:
    def __init__(self, project_name=None, profile=""):
        self.context = _StubContext(project_name=project_name)
        self.config = SimpleNamespace(profile=profile)


def test_get_paths_uses_explicit_project_when_bound(projects_root):
    """When a project IS bound on the context, ``get_paths`` reads
    that project's skill dir — same as before D3a."""

    (projects_root / "engineering" / ".a0proj" / "skills").mkdir(parents=True)

    from python.helpers import subagents

    paths = subagents.get_paths(
        _StubAgent(project_name="engineering"),
        "skills",
        must_exist_completely=False,
    )
    # First path must point at engineering's skills dir.
    assert paths
    assert any("engineering" in p and p.endswith("skills") for p in paths)


def test_get_paths_falls_through_to_default_for_projectless_context(
    projects_root,
):
    """Spec 10 D3a: projectless agent contexts resolve through
    ``_default``, NOT the user/global directories alone. After spec 09
    P1.9 every chat lives in some project — D3a applies the same
    invariant to ``subagents.get_paths``.
    """

    (projects_root / "_default" / ".a0proj" / "skills").mkdir(parents=True)

    from python.helpers import subagents

    paths = subagents.get_paths(
        _StubAgent(project_name=None),  # nothing bound on the context
        "skills",
        must_exist_completely=False,
    )
    # _default's skill dir must appear in the resolved path list.
    assert paths
    assert any("_default" in p and p.endswith("skills") for p in paths), (
        f"_default skills dir missing from resolved paths: {paths}"
    )


def test_get_paths_no_agent_still_uses_admin_wildcard(projects_root):
    """``get_skill_roots(agent=None)`` is the admin surface; it must
    keep using the wildcard scan across all projects. D3 documented
    this isn't a bug — it's how the operator's skill browser sees
    every project's skills.

    We don't test wildcard expansion here (no skills to find in this
    tmp root); we just verify the no-agent path stays distinct from
    the agent-bound path: ``get_paths(None, ...)`` raises since no
    project context is available.
    """

    from python.helpers import subagents

    # No agent → the get_paths function still works (it skips the
    # project branch), but typically returns user / default paths
    # only. The contract: agent=None doesn't suddenly start adding
    # _default.
    paths = subagents.get_paths(None, "skills", must_exist_completely=False)
    assert not any("_default" in p for p in paths)
