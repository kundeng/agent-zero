"""Tests for hyperagent0/projects.py — _default project bootstrap (spec 09)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperagent0 import projects as hp_projects


def test_resolve_project_name_returns_default_for_empty():
    assert hp_projects.resolve_project_name(None) == hp_projects.DEFAULT_PROJECT_NAME
    assert hp_projects.resolve_project_name("") == hp_projects.DEFAULT_PROJECT_NAME
    assert hp_projects.resolve_project_name("   ") == hp_projects.DEFAULT_PROJECT_NAME


def test_resolve_project_name_preserves_explicit_name():
    assert hp_projects.resolve_project_name("engineering") == "engineering"
    assert hp_projects.resolve_project_name(" foo ") == "foo"


def test_ensure_default_project_creates_skeleton(tmp_path, monkeypatch):
    """Bootstrap should create .a0proj/ with the minimum scaffold."""

    monkeypatch.setattr(
        hp_projects, "_project_root", lambda: tmp_path / "projects"
    )

    pdir = hp_projects.ensure_default_project()
    assert pdir == tmp_path / "projects" / hp_projects.DEFAULT_PROJECT_NAME

    meta = pdir / ".a0proj"
    assert meta.is_dir()
    assert (meta / "project.json").is_file()
    assert (meta / "instructions").is_dir()
    assert (meta / "knowledge").is_dir()
    assert (meta / "skills").is_dir()
    # secrets.env / mcp_servers.json NOT created (so globals apply).
    assert not (meta / "secrets.env").exists()
    assert not (meta / "mcp_servers.json").exists()


def test_ensure_default_project_idempotent(tmp_path, monkeypatch):
    """Multiple calls don't overwrite the existing project.json."""

    monkeypatch.setattr(
        hp_projects, "_project_root", lambda: tmp_path / "projects"
    )

    pdir = hp_projects.ensure_default_project()
    project_json = pdir / ".a0proj" / "project.json"
    # User edits the project.json
    project_json.write_text(
        json.dumps(
            {"title": "User Edited", "instructions": "be helpful"}, indent=2
        )
    )

    # Second call should NOT clobber the edit
    hp_projects.ensure_default_project()
    data = json.loads(project_json.read_text())
    assert data["title"] == "User Edited"
    assert data["instructions"] == "be helpful"


def test_ensure_default_project_records_workdir_path(tmp_path, monkeypatch):
    """When workdir_path is passed, it lands under the project_folder key.

    Per spec 09 D2: ``_default.project_folder = workdir_path`` is the
    handle that ``get_project_work_folder`` reads to redirect the
    sandbox/code-exec cwd to the operator's chosen workdir.
    """

    monkeypatch.setattr(
        hp_projects, "_project_root", lambda: tmp_path / "projects"
    )

    pdir = hp_projects.ensure_default_project(workdir_path="/tmp/my-workdir")
    data = json.loads((pdir / ".a0proj" / "project.json").read_text())
    assert data["project_folder"] == "/tmp/my-workdir"
    # Old key never present.
    assert "workdir_path" not in data


def test_default_project_has_sensible_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hp_projects, "_project_root", lambda: tmp_path / "projects"
    )

    pdir = hp_projects.ensure_default_project()
    data = json.loads((pdir / ".a0proj" / "project.json").read_text())
    assert data["title"] == "Default"
    assert data["instructions"] == ""
    assert data["git_url"] == ""
