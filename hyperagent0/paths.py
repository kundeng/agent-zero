"""Resolve the hyperagent-zero runtime tree.

Upstream agent-zero is not a proper Python package — ``agent.py``,
``run_ui.py``, and the asset directories (``prompts/``, ``agents/``,
``webui/`` ...) live at a project root and expect ``cwd`` or
``PYTHONPATH`` to point at it. The pip wheel only ships our
``hyperagent0/`` and ``python/`` packages, so a non-editable install
cannot ``import agent`` until we tell Python where the working tree is.

Resolution order:

1. ``$HYPERAGENT0_REPO`` environment variable.
2. Walk up from ``__file__`` looking for ``agent.py`` (catches editable
   installs and developer checkouts where this module is inside the
   source tree).
3. ``~/.hyperagent0/repo`` (where ``install.sh`` clones it by default).

The cold-start budget (spec 03 D5) bans heavy imports here — stdlib
only, and the lookup is cached.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path


class RepoNotFound(RuntimeError):
    """Raised when no candidate path contains a recognizable working tree."""


def _looks_like_repo(p: Path) -> bool:
    return (p / "agent.py").is_file() and (p / "pyproject.toml").is_file()


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """Return the directory containing ``agent.py`` and runtime assets."""

    env = os.environ.get("HYPERAGENT0_REPO")
    if env:
        candidate = Path(env).expanduser().resolve()
        if _looks_like_repo(candidate):
            return candidate
        raise RepoNotFound(
            f"HYPERAGENT0_REPO={env} does not contain agent.py and pyproject.toml"
        )

    # Editable installs and developer checkouts: this module lives inside
    # the source tree, so its parents include the repo root.
    here = Path(__file__).resolve()
    for parent in here.parents:
        if _looks_like_repo(parent):
            return parent

    default = Path.home() / ".hyperagent0" / "repo"
    if _looks_like_repo(default):
        return default

    raise RepoNotFound(
        "Cannot locate the hyperagent-zero working tree. Either set "
        "HYPERAGENT0_REPO, install via install.sh (which clones to "
        "~/.hyperagent0/repo), or run from inside a checkout."
    )


def settings_path() -> Path:
    """Path to ``usr/settings.json`` inside the repo."""

    return repo_root() / "usr" / "settings.json"


def ensure_on_syspath() -> None:
    """Add the repo to ``sys.path`` so ``import agent`` succeeds.

    Idempotent: prepends only if not already present. Silently does
    nothing if the repo cannot be located — subcommands that actually
    need ``agent`` will raise on import with a clearer error.
    """

    try:
        root = repo_root()
    except RepoNotFound:
        return
    spath = str(root)
    if spath not in sys.path:
        sys.path.insert(0, spath)
