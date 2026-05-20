"""DockerBackend — fresh container per project, never reuses the agent's container.

Bind-mounts the project directory RW (host path via :mod:`path_translate`)
and the project's ``knowledge/`` subdir RO. Resource limits and network
policy come from the per-project :class:`ProjectSandboxSettings`.

``docker`` Python SDK is **lazy-imported** inside :meth:`__init__` so the
import only fires when a project actually selects ``sandbox_mode=docker``.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from typing import Any, Dict, List, Optional, Union

from hyperagent0.sandbox import (
    ProjectSandboxSettings,
    SandboxBackend,
    register_backend,
)
from hyperagent0.sandbox import path_translate

logger = logging.getLogger(__name__)


DEFAULT_IMAGE = "hyperagent0/sandbox:latest"


def _network_mode_for(policy: Union[str, Dict[str, Any]]) -> str:
    """Map :class:`ProjectSandboxSettings.network` to Docker's ``network_mode``.

    Allowlist dicts collapse to ``bridge`` here — the actual allowlist is
    enforced by a follow-up iptables/nftables hook (spec-05 task 2.2).
    """
    if isinstance(policy, dict):
        return "bridge"
    if policy == "none":
        return "none"
    if policy == "local-only":
        return "host"
    return "bridge"


def _bind_mounts_for(project_dir: Optional[str]) -> Dict[str, Dict[str, str]]:
    """Return ``{host_path: {"bind": container_path, "mode": "rw"|"ro"}}`` mapping."""
    if not project_dir:
        return {}
    host_project = path_translate.to_host(project_dir)
    mounts: Dict[str, Dict[str, str]] = {
        host_project: {"bind": "/workspace", "mode": "rw"},
    }
    knowledge = os.path.join(project_dir, ".a0proj", "knowledge")
    if os.path.isdir(knowledge):
        host_knowledge = path_translate.to_host(knowledge)
        mounts[host_knowledge] = {"bind": "/workspace/.a0proj/knowledge", "mode": "ro"}
    return mounts


class DockerBackend(SandboxBackend):
    """Spawns a fresh Docker container per project."""

    mode = "docker"

    @classmethod
    def is_available(cls) -> bool:
        # We only check the CLI here; the SDK itself is a soft dep surfaced
        # at construction time with a friendly install hint.
        return shutil.which("docker") is not None

    def __init__(
        self,
        project_dir: Optional[str] = None,
        settings: Optional[ProjectSandboxSettings] = None,
    ) -> None:
        super().__init__(project_dir=project_dir, settings=settings)
        # LAZY IMPORT: do not import `docker` at module load time.
        try:
            import docker as docker_sdk  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "DockerBackend requires the 'docker' SDK. Install with: "
                "pip install 'hyperagent0[docker]'"
            ) from exc
        self._docker_sdk = docker_sdk
        self._client: Any = None
        self._container: Any = None

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    def _client_lazy(self) -> Any:
        if self._client is None:
            self._client = self._docker_sdk.from_env()
        return self._client

    def _container_kwargs(self) -> Dict[str, Any]:
        settings = self.settings or ProjectSandboxSettings()
        limits = settings.resource_limits
        kwargs: Dict[str, Any] = {
            "image": settings.image or DEFAULT_IMAGE,
            "name": f"hyperagent0-sandbox-{uuid.uuid4().hex[:12]}",
            "detach": True,
            "tty": True,
            "stdin_open": True,
            "working_dir": "/workspace",
            "command": ["sleep", "infinity"],
            "volumes": _bind_mounts_for(self.project_dir),
            "network_mode": _network_mode_for(settings.network),
            "auto_remove": not settings.persist_sandbox,
        }
        if limits.memory:
            kwargs["mem_limit"] = limits.memory
        if limits.cpus is not None and limits.cpus > 0:
            # docker-py uses nano_cpus = cpus * 1e9.
            kwargs["nano_cpus"] = int(limits.cpus * 1_000_000_000)
        return kwargs

    def _start_container(self) -> Any:
        if self._container is not None:
            return self._container
        client = self._client_lazy()
        self._container = client.containers.run(**self._container_kwargs())
        logger.info(
            "started %s container %s for project %s",
            self.mode,
            getattr(self._container, "name", "?"),
            self.project_dir,
        )
        return self._container

    # ------------------------------------------------------------------
    # SandboxBackend protocol
    # ------------------------------------------------------------------

    async def open_shell(self, cwd: str) -> Any:
        container = self._start_container()
        # Shape a minimal duck-typed session matching LocalInteractiveSession
        # so call sites stay backend-agnostic. Spec 01's merged ABC will
        # tighten this contract.
        return _DockerExecSession(container=container, cwd=cwd)

    async def close(self) -> None:
        if self._container is not None:
            try:
                self._container.stop(timeout=2)
            except Exception:  # pragma: no cover — best-effort cleanup
                logger.debug("error stopping container", exc_info=True)
            finally:
                self._container = None


class _DockerExecSession:
    """Minimal interactive session over ``docker exec`` (placeholder).

    The full streaming impl will share code with ``SSHInteractiveSession``;
    here we expose the surface so the registry and lifecycle code compile.
    """

    def __init__(self, container: Any, cwd: str) -> None:
        self._container = container
        self.cwd = cwd
        self._last_output = ""

    async def connect(self) -> None:  # pragma: no cover — placeholder
        return None

    async def close(self) -> None:  # pragma: no cover — placeholder
        return None

    async def send_command(self, command: str) -> None:  # pragma: no cover
        exit_code, output = self._container.exec_run(
            cmd=["bash", "-lc", command],
            workdir=self.cwd,
            tty=True,
            demux=False,
        )
        self._last_output = output.decode("utf-8", errors="replace") if output else ""

    async def read_output(self, timeout: float = 0, reset_full_output: bool = False):  # pragma: no cover
        out = self._last_output
        if reset_full_output:
            self._last_output = ""
        return out, None


register_backend("docker", DockerBackend)


__all__ = ["DockerBackend"]
