"""Spec 10 P1.2 — per-project MCP resolver.

``hyperagent0.mcp.get_mcp_config_for_agent`` is the indirection that
runtime consumers (system prompt + tool dispatch) use in place of the
raw ``MCPConfig.get_instance()``. Contract:

* No per-project ``mcp_servers.json`` → return the global singleton.
* Per-project payload present → return a cached per-project ``MCPConfig``.
* Repeat calls for the same project → same cached instance.
* Different projects → different instances.

We don't spawn real MCP servers in these tests; the resolver builds
its per-project ``MCPConfig`` in a worker thread, and we monkeypatch
the ``MCPConfig`` constructor to record what payload it received
rather than actually fetching tool lists over the network.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    """Redirect ``hyperagent0.projects._project_root`` to a tmp tree
    so the resolver reads from a clean per-test directory.
    """

    root = tmp_path / "usr" / "projects"
    root.mkdir(parents=True)

    from hyperagent0 import projects as hp_projects

    monkeypatch.setattr(hp_projects, "_project_root", lambda: root)
    return root


@pytest.fixture(autouse=True)
def _clear_resolver_cache():
    """Each test gets a fresh resolver cache; otherwise project names
    leak between cases."""

    from hyperagent0 import mcp as project_mcp

    project_mcp.reset_cache()
    yield
    project_mcp.reset_cache()


@pytest.fixture
def fake_mcpconfig(monkeypatch):
    """Replace upstream ``MCPConfig`` constructor with a stub that
    records the ``servers_list`` it was passed. Avoids spawning real
    MCP subprocesses / network calls in tests.

    Returns a list that captures the servers_list of each constructed
    instance, so a test can assert "this project got these servers".
    """

    constructed: list[list[dict]] = []

    class _StubMCPConfig:
        """Mimics the subset of MCPConfig the resolver / consumers touch."""

        _singleton: Any = None

        def __init__(self, servers_list):
            self.servers = list(servers_list)
            self._servers_list = list(servers_list)
            constructed.append(list(servers_list))

        @classmethod
        def get_instance(cls):
            if cls._singleton is None:
                cls._singleton = cls(servers_list=[{"name": "global_stub"}])
            return cls._singleton

        @staticmethod
        def normalize_config(servers):
            # Mirror the real classmethod's list-of-dicts contract.
            if isinstance(servers, list):
                return [s for s in servers if isinstance(s, dict)]
            if isinstance(servers, dict):
                if "mcpServers" in servers and isinstance(servers["mcpServers"], dict):
                    out = []
                    for k, v in servers["mcpServers"].items():
                        if isinstance(v, dict):
                            v = {**v, "name": k}
                            out.append(v)
                    return out
            return []

        def get_tools_prompt(self, server_name: str = "") -> str:
            return f"tools: {[s.get('name') for s in self.servers]}"

        def get_tool(self, agent, tool_name):
            return None

    # Reset class-level singleton state across tests.
    _StubMCPConfig._singleton = None

    import python.helpers.mcp_handler as mcp_handler

    monkeypatch.setattr(mcp_handler, "MCPConfig", _StubMCPConfig)
    return constructed


def _stub_agent(project_name=None):
    from python.helpers.projects import CONTEXT_DATA_KEY_PROJECT

    class _Ctx:
        def get_data(self, key):
            if key == CONTEXT_DATA_KEY_PROJECT:
                return project_name
            return None

    return SimpleNamespace(context=_Ctx())


def test_resolver_returns_global_when_no_project_mcp_file(
    projects_root, fake_mcpconfig
):
    """A project without ``mcp_servers.json`` falls through to the
    upstream global singleton — preserving pre-spec-10 behavior."""

    from hyperagent0.mcp import get_mcp_config_for_agent
    from python.helpers.mcp_handler import MCPConfig

    agent = _stub_agent(project_name="engineering")
    cfg = get_mcp_config_for_agent(agent)

    assert cfg is MCPConfig.get_instance()
    # Stub global was constructed once; no per-project construction.
    assert len(fake_mcpconfig) == 1
    assert fake_mcpconfig[0] == [{"name": "global_stub"}]


def test_resolver_falls_through_for_empty_mcp_file(
    projects_root, fake_mcpconfig
):
    """An empty / whitespace-only file is the documented signal for
    fall-through (matches ``load_project_mcp_servers`` contract)."""

    pdir = projects_root / "blank" / ".a0proj"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "mcp_servers.json").write_text("   \n")

    from hyperagent0.mcp import get_mcp_config_for_agent
    from python.helpers.mcp_handler import MCPConfig

    cfg = get_mcp_config_for_agent(_stub_agent("blank"))
    assert cfg is MCPConfig.get_instance()


def test_resolver_returns_project_specific_when_file_present(
    projects_root, fake_mcpconfig
):
    """A project with ``mcp_servers.json`` gets its own MCPConfig
    constructed from that file's servers — NOT the global instance."""

    pdir = projects_root / "engineering" / ".a0proj"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "mcp_servers.json").write_text(
        '[{"name": "linear", "command": "npx", "args": ["-y", "linear"]}]'
    )

    from hyperagent0.mcp import get_mcp_config_for_agent
    from python.helpers.mcp_handler import MCPConfig

    cfg = get_mcp_config_for_agent(_stub_agent("engineering"))

    assert cfg is not MCPConfig.get_instance()
    # Per-project servers list reflects the project file.
    assert [s.get("name") for s in cfg.servers] == ["linear"]


def test_resolver_caches_per_project(projects_root, fake_mcpconfig):
    """Two calls for the same project return the SAME instance — the
    per-project MCPConfig is built once and reused."""

    pdir = projects_root / "engineering" / ".a0proj"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "mcp_servers.json").write_text(
        '[{"name": "linear", "command": "npx", "args": []}]'
    )

    from hyperagent0.mcp import get_mcp_config_for_agent

    first = get_mcp_config_for_agent(_stub_agent("engineering"))
    second = get_mcp_config_for_agent(_stub_agent("engineering"))

    assert first is second
    # Construction count: one global + one project = 2.
    assert len(fake_mcpconfig) == 2


def test_resolver_returns_distinct_instances_per_project(
    projects_root, fake_mcpconfig
):
    """Two different projects with mcp_servers.json get two different
    cached instances — they don't share."""

    for name, server in [("alpha", "tool-a"), ("beta", "tool-b")]:
        pdir = projects_root / name / ".a0proj"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "mcp_servers.json").write_text(
            f'[{{"name": "{server}", "command": "npx", "args": []}}]'
        )

    from hyperagent0.mcp import get_mcp_config_for_agent

    cfg_a = get_mcp_config_for_agent(_stub_agent("alpha"))
    cfg_b = get_mcp_config_for_agent(_stub_agent("beta"))

    assert cfg_a is not cfg_b
    assert [s.get("name") for s in cfg_a.servers] == ["tool-a"]
    assert [s.get("name") for s in cfg_b.servers] == ["tool-b"]


def test_resolver_falls_back_to_global_when_project_build_fails(
    projects_root, fake_mcpconfig, monkeypatch
):
    """A malformed project file or a constructor that raises must not
    bring down MCP for everyone — fall back to global and log."""

    pdir = projects_root / "broken" / ".a0proj"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "mcp_servers.json").write_text("[]")  # well-formed but empty

    # Force the project build path to raise.
    import hyperagent0.mcp as project_mcp

    def _boom(payload):
        raise RuntimeError("simulated construction failure")

    monkeypatch.setattr(project_mcp, "_build_project_mcp_config", _boom)
    # _build_project_mcp_config is gated by `load_project_mcp_servers`
    # returning a truthy payload, so we also need a non-empty file:
    (pdir / "mcp_servers.json").write_text(
        '[{"name": "x", "command": "echo"}]'
    )

    from hyperagent0.mcp import get_mcp_config_for_agent
    from python.helpers.mcp_handler import MCPConfig

    cfg = get_mcp_config_for_agent(_stub_agent("broken"))
    assert cfg is MCPConfig.get_instance()


def test_resolver_projectless_context_uses_default_fallthrough(
    projects_root, fake_mcpconfig
):
    """Spec 09 P1.9 invariant: a projectless context resolves through
    ``_default``. If ``_default`` has no MCP file, fall through to
    global — same as any other empty project."""

    # No mcp_servers.json anywhere; _default doesn't even exist yet.
    from hyperagent0.mcp import get_mcp_config_for_agent
    from python.helpers.mcp_handler import MCPConfig

    cfg = get_mcp_config_for_agent(_stub_agent(project_name=None))
    assert cfg is MCPConfig.get_instance()


def test_resolver_default_project_with_mcp_file_overrides_global(
    projects_root, fake_mcpconfig
):
    """If the operator drops an ``mcp_servers.json`` into ``_default``,
    projectless contexts pick it up (because they resolve through
    ``_default``)."""

    pdir = projects_root / "_default" / ".a0proj"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "mcp_servers.json").write_text(
        '[{"name": "default_only", "command": "echo"}]'
    )

    from hyperagent0.mcp import get_mcp_config_for_agent
    from python.helpers.mcp_handler import MCPConfig

    cfg = get_mcp_config_for_agent(_stub_agent(project_name=None))
    assert cfg is not MCPConfig.get_instance()
    assert [s.get("name") for s in cfg.servers] == ["default_only"]


def test_reset_cache_drops_project_instance(projects_root, fake_mcpconfig):
    """``reset_cache`` lets the resolver rebuild — useful for tests
    and (eventually) a 'reload MCP' UI action."""

    pdir = projects_root / "eng" / ".a0proj"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "mcp_servers.json").write_text(
        '[{"name": "v1", "command": "echo"}]'
    )

    from hyperagent0 import mcp as project_mcp
    from hyperagent0.mcp import get_mcp_config_for_agent

    first = get_mcp_config_for_agent(_stub_agent("eng"))

    # Change the file; without reset, cache still serves the old build.
    (pdir / "mcp_servers.json").write_text(
        '[{"name": "v2", "command": "echo"}]'
    )
    cached = get_mcp_config_for_agent(_stub_agent("eng"))
    assert cached is first
    assert [s.get("name") for s in cached.servers] == ["v1"]

    project_mcp.reset_cache("eng")

    rebuilt = get_mcp_config_for_agent(_stub_agent("eng"))
    assert rebuilt is not first
    assert [s.get("name") for s in rebuilt.servers] == ["v2"]
