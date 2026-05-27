"""Spec 10 P2 — backend writers + API actions for the per-project
MCP and network allowlist editors.

The writer helpers live in :mod:`hyperagent0.projects`:

* :func:`save_project_mcp_servers` — write / delete
  ``.a0proj/mcp_servers.json`` and invalidate the resolver cache.
* :func:`save_project_network_allow` — merge a new ``network.allow``
  list into ``project.json`` while preserving other top-level keys.

The API actions live in :class:`python.api.projects.Projects`:

* ``mcp_get`` / ``mcp_set``
* ``network_get`` / ``network_set``

These tests exercise the helpers directly (file-system contract)
and the API actions as plain method calls (route-handler contract);
the HTTP layer above the action dispatch is upstream code and
out of scope here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    """Redirect both ``hyperagent0.projects._project_root`` and the
    upstream ``files`` resolver to a tmp tree so writes land in a
    sandbox and don't pollute the dev repo's ``usr/projects/``."""

    root = tmp_path / "usr" / "projects"
    root.mkdir(parents=True)

    from hyperagent0 import projects as hp_projects

    monkeypatch.setattr(hp_projects, "_project_root", lambda: root)

    from python.helpers import projects as up_projects

    monkeypatch.setattr(
        up_projects, "get_projects_parent_folder", lambda: str(root)
    )
    return root


# ---------------------------------------------------------------------------
# save_project_mcp_servers — write / delete / cache invalidation
# ---------------------------------------------------------------------------


def test_save_mcp_servers_writes_payload(projects_root):
    payload = '[{"name": "linear", "command": "npx", "args": ["-y", "linear"]}]'

    from hyperagent0.projects import (
        load_project_mcp_servers,
        save_project_mcp_servers,
    )

    save_project_mcp_servers("eng", payload)

    written = (projects_root / "eng" / ".a0proj" / "mcp_servers.json").read_text()
    # Persistence preserves the JSON text (trailing newline trimmed + readded).
    assert written.rstrip() == payload.rstrip()
    # Round-trip through the read helper.
    assert load_project_mcp_servers("eng").rstrip() == payload.rstrip()


def test_save_mcp_servers_creates_meta_dir_if_missing(projects_root):
    """Writer must not fail on a project that has no ``.a0proj/`` yet —
    the UI flow lets you set MCP on a project before any other edit."""

    from hyperagent0.projects import save_project_mcp_servers

    save_project_mcp_servers("fresh", '[{"name": "x", "command": "echo"}]')

    assert (projects_root / "fresh" / ".a0proj" / "mcp_servers.json").exists()


def test_save_mcp_servers_empty_payload_deletes_file(projects_root):
    """Empty/None payload = the documented 'fall through to global' signal.

    The reader returns ``None`` when the file is missing, so the writer's
    job for empty payloads is to *delete* the file rather than leave a
    zero-length one (which the reader would also treat as None, but
    leaves stale state on disk that looks intentional)."""

    from hyperagent0.projects import (
        load_project_mcp_servers,
        save_project_mcp_servers,
    )

    save_project_mcp_servers("temp", '[{"name": "x", "command": "echo"}]')
    assert (projects_root / "temp" / ".a0proj" / "mcp_servers.json").exists()

    save_project_mcp_servers("temp", "")
    assert not (projects_root / "temp" / ".a0proj" / "mcp_servers.json").exists()
    assert load_project_mcp_servers("temp") is None

    # None is also a valid empty signal.
    save_project_mcp_servers("temp", '[{"name": "y", "command": "echo"}]')
    save_project_mcp_servers("temp", None)
    assert not (projects_root / "temp" / ".a0proj" / "mcp_servers.json").exists()


def test_save_mcp_servers_rejects_malformed_json(projects_root):
    """Validation at the editor boundary — the upstream MCPConfig
    parser is lenient, but the editor must surface obvious typos before
    a daemon-side error appears."""

    from hyperagent0.projects import save_project_mcp_servers

    with pytest.raises(ValueError, match="valid JSON"):
        save_project_mcp_servers("eng", "{not valid json")

    # No file was created on validation failure.
    assert not (projects_root / "eng" / ".a0proj" / "mcp_servers.json").exists()


def test_save_mcp_servers_invalidates_resolver_cache(projects_root, monkeypatch):
    """A successful write must drop any cached per-project MCPConfig
    so the next ``get_mcp_config_for_agent`` call rebuilds. Without
    this, the editor would only take effect after a daemon restart."""

    cleared: list[str] = []

    def _fake_reset_cache(project_name=None):
        cleared.append(project_name or "*ALL*")

    from hyperagent0 import mcp as project_mcp

    monkeypatch.setattr(project_mcp, "reset_cache", _fake_reset_cache)

    from hyperagent0.projects import save_project_mcp_servers

    save_project_mcp_servers("eng", '[{"name": "x", "command": "echo"}]')
    assert cleared == ["eng"]

    save_project_mcp_servers("eng", "")  # delete also invalidates
    assert cleared == ["eng", "eng"]


# ---------------------------------------------------------------------------
# save_project_network_allow — preserve other project.json keys
# ---------------------------------------------------------------------------


def test_save_network_allow_creates_project_json_if_missing(projects_root):
    from hyperagent0.projects import (
        load_project_network_allow,
        save_project_network_allow,
    )

    written = save_project_network_allow("fresh", ["github.com"])
    assert written == ["github.com"]

    pj = json.loads(
        (projects_root / "fresh" / ".a0proj" / "project.json").read_text()
    )
    assert pj["network"]["allow"] == ["github.com"]
    assert load_project_network_allow("fresh") == ["github.com"]


def test_save_network_allow_preserves_other_keys(projects_root):
    """Spec 10 D2: writing the allowlist must not clobber ``title``,
    ``description``, ``project_folder``, or any other top-level keys —
    the network section is one field among many."""

    meta = projects_root / "eng" / ".a0proj"
    meta.mkdir(parents=True)
    (meta / "project.json").write_text(
        json.dumps(
            {
                "title": "Engineering",
                "description": "the eng workspace",
                "color": "#445566",
                "project_folder": "/home/u/eng-workdir",
                "network": {"allow": ["old.com"]},
            },
            indent=2,
        )
    )

    from hyperagent0.projects import save_project_network_allow

    save_project_network_allow("eng", ["github.com", "*.example.internal"])

    pj = json.loads((meta / "project.json").read_text())
    assert pj["title"] == "Engineering"
    assert pj["description"] == "the eng workspace"
    assert pj["color"] == "#445566"
    assert pj["project_folder"] == "/home/u/eng-workdir"
    assert pj["network"]["allow"] == ["github.com", "*.example.internal"]


def test_save_network_allow_filters_non_string_and_blank_entries(projects_root):
    """Defensive — a hand-edited form could submit empties/non-strings."""

    from hyperagent0.projects import save_project_network_allow

    written = save_project_network_allow(
        "eng", ["good.com", "", "   ", None, 42, "  trimmed.com  "]  # type: ignore[list-item]
    )
    assert written == ["good.com", "trimmed.com"]


def test_save_network_allow_overwrites_existing_network_allow(projects_root):
    """The editor is a *set* operation — old hosts that aren't in the
    new submission are removed (not merged). Layered union with the
    global default happens at sandbox boot, not at write time."""

    from hyperagent0.projects import save_project_network_allow

    save_project_network_allow("eng", ["a.com", "b.com"])
    save_project_network_allow("eng", ["c.com"])

    pj = json.loads(
        (projects_root / "eng" / ".a0proj" / "project.json").read_text()
    )
    assert pj["network"]["allow"] == ["c.com"]


def test_save_network_allow_handles_malformed_existing_project_json(
    projects_root,
):
    """If ``project.json`` is corrupt, the writer rewrites it from
    scratch with just the network section. Better than crashing and
    blocking the editor flow."""

    meta = projects_root / "broken" / ".a0proj"
    meta.mkdir(parents=True)
    (meta / "project.json").write_text("{not valid json")

    from hyperagent0.projects import save_project_network_allow

    save_project_network_allow("broken", ["recovery.com"])

    pj = json.loads((meta / "project.json").read_text())
    assert pj == {"network": {"allow": ["recovery.com"]}}


# ---------------------------------------------------------------------------
# API actions — Projects.set_project_mcp / .set_project_network etc.
# ---------------------------------------------------------------------------


def _make_projects_handler():
    """Construct a ``Projects`` handler instance that skips the
    upstream ``ApiHandler.__init__`` (which wants Flask app state),
    so the editor methods can be called directly in unit tests.
    """

    from python.api.projects import Projects

    return Projects.__new__(Projects)


class _StubProjectsHandler:
    """Lightweight call dispatcher that routes ``call(action, **kw)``
    to the matching method on a real (bare-constructed) ``Projects``
    instance. Mirrors what the upstream HTTP layer does without
    pulling in Flask plumbing."""

    _ACTION_MAP = {
        "mcp_get": "get_project_mcp",
        "mcp_set": "set_project_mcp",
        "network_get": "get_project_network",
        "network_set": "set_project_network",
    }

    def __init__(self):
        self._handler = _make_projects_handler()

    def call(self, action: str, **kwargs):
        method = getattr(self._handler, self._ACTION_MAP[action])
        return method(**kwargs)


def test_api_mcp_get_reports_global_fallthrough_when_no_file(projects_root):
    handler = _StubProjectsHandler()

    result = handler.call("mcp_get", name="eng")
    assert result == {"name": "eng", "payload": "", "uses_global": True}


def test_api_mcp_set_writes_then_get_returns_payload(projects_root):
    handler = _StubProjectsHandler()
    payload = '[{"name": "linear", "command": "npx", "args": []}]'

    written = handler.call("mcp_set", name="eng", payload=payload)
    assert written["uses_global"] is False
    assert written["payload"].rstrip() == payload

    fetched = handler.call("mcp_get", name="eng")
    assert fetched["payload"].rstrip() == payload


def test_api_mcp_set_with_empty_payload_falls_through(projects_root):
    handler = _StubProjectsHandler()

    handler.call("mcp_set", name="eng", payload='[{"name": "x", "command": "echo"}]')
    after_delete = handler.call("mcp_set", name="eng", payload="")
    assert after_delete == {"name": "eng", "payload": "", "uses_global": True}


def test_api_mcp_set_surfaces_json_validation_error(projects_root):
    handler = _StubProjectsHandler()

    with pytest.raises(Exception, match="valid JSON"):
        handler.call("mcp_set", name="eng", payload="{not valid")


def test_api_network_get_reports_empty_when_no_file(projects_root):
    handler = _StubProjectsHandler()
    assert handler.call("network_get", name="eng") == {
        "name": "eng",
        "allow": [],
    }


def test_api_network_set_round_trips_and_normalizes(projects_root):
    handler = _StubProjectsHandler()

    written = handler.call(
        "network_set",
        name="eng",
        allow=["github.com", "  whitespace.com  ", "", None],  # type: ignore[list-item]
    )
    assert written["allow"] == ["github.com", "whitespace.com"]

    fetched = handler.call("network_get", name="eng")
    assert fetched["allow"] == ["github.com", "whitespace.com"]


def test_api_network_set_rejects_non_list(projects_root):
    handler = _StubProjectsHandler()

    with pytest.raises(Exception, match="allow must be a list"):
        handler.call("network_set", name="eng", allow="github.com")


def test_api_actions_require_project_name(projects_root):
    handler = _StubProjectsHandler()

    for action in ("mcp_get", "network_get"):
        with pytest.raises(Exception, match="Project name is required"):
            handler.call(action, name=None)

    with pytest.raises(Exception, match="Project name is required"):
        handler.call("mcp_set", name=None, payload="[]")

    with pytest.raises(Exception, match="Project name is required"):
        handler.call("network_set", name=None, allow=[])
