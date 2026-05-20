"""Deployment-mode runtime helpers (spec 01-host-first task 1.2)."""

from .deployment_mode import (
    DEPLOYMENT_MODE_DOCKER,
    DEPLOYMENT_MODE_HOST,
    is_docker_mode,
    is_host_mode,
    resolve_deployment_mode,
)

__all__ = [
    "DEPLOYMENT_MODE_DOCKER",
    "DEPLOYMENT_MODE_HOST",
    "is_docker_mode",
    "is_host_mode",
    "resolve_deployment_mode",
]
