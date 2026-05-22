"""Wheel-content invariants.

The wheel MUST ship our wrapper ``hyperagent0/`` only. The upstream
``python/`` package — and the project's runtime asset directories
(``prompts/``, ``agents/``, ``webui/``, etc.) — MUST NOT be bundled,
because their relative-path lookups would resolve to ``site-packages/``
in a non-editable install. install.sh's git clone + .pth file is the
mechanism that puts them on disk where the runtime expects them.

See spec 07 D4 for the full rationale; this test pins the contract.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    """Build the wheel once per test session into a tmpdir."""

    out_dir = tmp_path_factory.mktemp("wheel")
    # ``pip wheel`` builds without installing. ``--no-deps`` keeps the
    # build self-contained (we don't need browser-use et al. on this
    # invocation just to assert the contents of our own wheel).
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(out_dir),
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            f"wheel build failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    wheels = list(out_dir.glob("hyperagent0-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def _members(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as z:
        return z.namelist()


def test_wheel_includes_hyperagent0_package(built_wheel):
    members = _members(built_wheel)
    assert any(
        m.startswith("hyperagent0/") and m.endswith("__init__.py") for m in members
    ), "hyperagent0/__init__.py missing from wheel"
    # Sentinel module we wrote in spec 07 — pins the runtime resolver.
    assert "hyperagent0/paths.py" in members, "hyperagent0/paths.py missing"


def test_wheel_excludes_upstream_python_package(built_wheel):
    """The python/ upstream package MUST NOT be in the wheel."""

    members = _members(built_wheel)
    leaked = [m for m in members if m.startswith("python/")]
    assert not leaked, (
        f"wheel leaked {len(leaked)} files from python/: "
        f"first 5: {leaked[:5]}. "
        "See spec 07 D4 — the upstream python/ package MUST stay in the "
        "cloned repo on disk, not in site-packages."
    )


def test_wheel_excludes_runtime_asset_directories(built_wheel):
    """Same trap as python/: asset dirs would resolve to site-packages."""

    members = _members(built_wheel)
    forbidden_prefixes = (
        "prompts/",
        "agents/",
        "knowledge/",
        "webui/",
        "skills/",
        "conf/",
        "lib/",
        "docker/",
        "docs/",
        "tests/",
        "usr/",
    )
    for prefix in forbidden_prefixes:
        leaked = [m for m in members if m.startswith(prefix)]
        assert not leaked, f"wheel leaked {len(leaked)} files from {prefix}"


def test_wheel_registers_haz_and_hyperagent0_entry_points(built_wheel):
    """Both console_scripts must be present so installs get a working CLI."""

    with zipfile.ZipFile(built_wheel) as z:
        entry_files = [m for m in z.namelist() if m.endswith("entry_points.txt")]
        assert entry_files, f"no entry_points.txt in wheel: {z.namelist()[:10]}"
        content = z.read(entry_files[0]).decode()
    assert "haz = hyperagent0.cli:main" in content, content
    assert "hyperagent0 = hyperagent0.cli:main" in content, content


def test_wheel_declares_click_runtime_dep(built_wheel):
    """The fork's only added runtime dep (click) must be declared."""

    with zipfile.ZipFile(built_wheel) as z:
        meta = [m for m in z.namelist() if m.endswith("METADATA")]
        assert meta, "no METADATA in wheel"
        content = z.read(meta[0]).decode()
    assert any(
        line.startswith("Requires-Dist: click")
        for line in content.splitlines()
    ), "click not declared as a runtime dep"
