"""Detect the agent's deployment mode (host vs docker).

Spec 01-host-first D1: host-first by default. We support three signals,
checked in order:

1. ``DEPLOYMENT_MODE`` env var — explicit, wins over everything.
2. ``--dockerized`` CLI flag — preserves upstream behavior, still wins
   over auto-detect.
3. ``/.dockerenv`` presence — Docker's own marker; auto-detects existing
   containerized deployments without user intervention.

Decoupling :func:`is_development` from container detection is part of the
same task (D-decision in open questions). Development mode is now opt-in
via ``A0_DEV=1`` env var or the ``--development`` CLI flag, instead of
being inferred as "not in docker".

The public surface is intentionally tiny and free of side effects so that
``python/helpers/runtime.py`` can delegate without import cycles.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import Literal

DEPLOYMENT_MODE_HOST = "host"
DEPLOYMENT_MODE_DOCKER = "docker"

DeploymentMode = Literal["host", "docker"]


def _read_env_mode() -> DeploymentMode | None:
    raw = os.environ.get("DEPLOYMENT_MODE")
    if not raw:
        return None
    value = raw.strip().lower()
    if value in (DEPLOYMENT_MODE_HOST, DEPLOYMENT_MODE_DOCKER):
        return value  # type: ignore[return-value]
    return None


def _read_cli_dockerized() -> bool | None:
    """Return True if --dockerized appears in argv, else None.

    We avoid importing python.helpers.runtime.args here to keep this
    module side-effect-free and importable at any point in the startup
    sequence.
    """
    for arg in sys.argv[1:]:
        # Accept both --dockerized and --dockerized=true forms.
        if arg == "--dockerized":
            return True
        if arg.startswith("--dockerized="):
            value = arg.split("=", 1)[1].strip().lower()
            if value in ("true", "1", "yes", "on"):
                return True
            if value in ("false", "0", "no", "off"):
                return False
    return None


def _has_dockerenv_marker() -> bool:
    try:
        return os.path.exists("/.dockerenv")
    except OSError:
        return False


@lru_cache(maxsize=1)
def resolve_deployment_mode() -> DeploymentMode:
    """Resolve the active deployment mode once per process.

    Resolution order: env var → CLI flag → /.dockerenv → host default.
    """
    env_mode = _read_env_mode()
    if env_mode is not None:
        return env_mode

    cli_flag = _read_cli_dockerized()
    if cli_flag is True:
        return DEPLOYMENT_MODE_DOCKER
    if cli_flag is False:
        return DEPLOYMENT_MODE_HOST

    if _has_dockerenv_marker():
        return DEPLOYMENT_MODE_DOCKER

    return DEPLOYMENT_MODE_HOST


def is_host_mode() -> bool:
    return resolve_deployment_mode() == DEPLOYMENT_MODE_HOST


def is_docker_mode() -> bool:
    return resolve_deployment_mode() == DEPLOYMENT_MODE_DOCKER
