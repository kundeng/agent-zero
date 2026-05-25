"""Behavior tests for spec 09 P1.9: projectless ↔ ``_default`` collapse.

Each of the four historically-branched sites (system prompt, code-exec
``ensure_cwd`` / ``_active_project_dir``, secrets manager, sandbox
profile) must now produce the same result when invoked with no project
binding as when invoked against an explicitly-bound ``_default``.

These tests work directly against the helpers — they avoid spinning up
a real ``Agent`` so they stay fast and don't pull LiteLLM / Flask /
channel SDK imports.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Shared fixture: a tmp project root with a ``_default`` skeleton.
# ---------------------------------------------------------------------------


@pytest.fixture
def default_project(tmp_path, monkeypatch):
    """Point both project resolvers at a tmp root and seed ``_default``."""

    projects_root = tmp_path / "usr" / "projects"
    projects_root.mkdir(parents=True)

    # Redirect hyperagent0's bootstrap helper to the tmp tree.
    from hyperagent0 import projects as hp_projects

    monkeypatch.setattr(hp_projects, "_project_root", lambda: projects_root)

    # Redirect upstream's resolver so ``get_project_folder("_default")``
    # also lands under the tmp tree.
    from python.helpers import projects as up_projects

    monkeypatch.setattr(
        up_projects,
        "get_projects_parent_folder",
        lambda: str(projects_root),
    )

    # Bootstrap ``_default``. Records ``project_folder`` = workdir_path.
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    hp_projects.ensure_default_project(workdir_path=str(workdir))

    return SimpleNamespace(
        projects_root=projects_root,
        workdir=workdir,
        default_meta=projects_root / "_default" / ".a0proj",
    )


# ---------------------------------------------------------------------------
# Bootstrap: ensure_default_project writes project_folder, not workdir_path.
# ---------------------------------------------------------------------------


def test_default_project_records_project_folder_override(default_project):
    data = json.loads((default_project.default_meta / "project.json").read_text())
    assert data["project_folder"] == str(default_project.workdir)
    # Old key never present.
    assert "workdir_path" not in data


# ---------------------------------------------------------------------------
# get_project_work_folder honors override.
# ---------------------------------------------------------------------------


def test_get_project_work_folder_returns_override_when_set(default_project):
    from python.helpers import projects as up_projects

    work = up_projects.get_project_work_folder("_default")
    assert work == str(default_project.workdir)


def test_get_project_work_folder_falls_back_when_no_override(
    default_project, tmp_path
):
    """Projects without a ``project_folder`` override keep the canonical path."""

    from python.helpers import projects as up_projects

    named_dir = default_project.projects_root / "engineering" / ".a0proj"
    named_dir.mkdir(parents=True)
    (named_dir / "project.json").write_text(
        json.dumps({"title": "Engineering"})
    )

    work = up_projects.get_project_work_folder("engineering")
    # Same as get_project_folder: no override → canonical usr/projects/<name>/
    assert work == up_projects.get_project_folder("engineering")


def test_get_project_meta_folder_is_not_affected_by_override(default_project):
    """``.a0proj/`` always lives under ``usr/projects/<name>/``."""

    from python.helpers import projects as up_projects

    meta = up_projects.get_project_meta_folder("_default")
    # Meta path must stay under the projects parent — never the override.
    assert meta.startswith(str(default_project.projects_root))
    assert str(default_project.workdir) not in meta


# ---------------------------------------------------------------------------
# Site 1: system_prompt collapse — ``resolve_project_name`` always picks
# a project; ``get_project_prompt`` always renders the active template.
# ---------------------------------------------------------------------------


def test_system_prompt_resolves_projectless_to_default():
    """``resolve_project_name(None)`` is the contract the system-prompt
    extension uses to never branch on ``if project_name:``.
    """

    from hyperagent0.projects import DEFAULT_PROJECT_NAME, resolve_project_name

    assert resolve_project_name(None) == DEFAULT_PROJECT_NAME
    assert resolve_project_name("") == DEFAULT_PROJECT_NAME


def test_build_system_prompt_vars_uses_work_folder(default_project):
    """``project_path`` in the prompt vars resolves through the work
    folder, so the override surfaces in the rendered ``active.md``.
    """

    from python.helpers import projects as up_projects

    vars_ = up_projects.build_system_prompt_vars("_default")
    # Match the workdir we migrated into project_folder. ``normalize_a0_path``
    # is a no-op in host mode (see CLAUDE.md), so the path round-trips.
    assert str(default_project.workdir) in vars_["project_path"]


# ---------------------------------------------------------------------------
# Site 3: secrets — the projectless branch is gone. Same context with no
# project binding still produces a usable SecretsManager (reads merge an
# empty per-project secrets.env).
# ---------------------------------------------------------------------------


def test_get_secrets_manager_projectless_resolves_default(
    default_project, monkeypatch
):
    """A context with no project binding now adds the ``_default`` secrets
    file path. Missing file → empty merge → same effective content as the
    old projectless branch.
    """

    from python.helpers import secrets

    class _StubContext:
        def get_data(self, key: str) -> Any:
            return None  # no project bound

    mgr = secrets.get_secrets_manager(_StubContext())  # type: ignore[arg-type]
    # _default's secrets.env path must appear in the file list (proof
    # that the collapse happened — no early-return).
    paths = list(mgr._files)
    assert any("_default" in p and p.endswith("secrets.env") for p in paths)


# ---------------------------------------------------------------------------
# Site 4: srt — _build_profile derives the write allowlist from the
# work folder. With the override migration, the projectless code path
# (project_dir = workdir override) ends up with the same allowlist as
# an explicit _default binding.
# ---------------------------------------------------------------------------


def test_srt_profile_uses_default_work_folder_when_collapsed(default_project):
    from hyperagent0.sandbox import srt
    from python.helpers import projects as up_projects

    work_dir = up_projects.get_project_work_folder("_default")

    profile = srt._build_profile(work_dir)
    assert profile["fs"]["write"]["allow"] == [work_dir]
    # ...and that's exactly what the migrated workdir is.
    assert work_dir == str(default_project.workdir)
