"""PodmanBackend — rootless container per project via Podman.

Same interface as :class:`hyperagent0.sandbox.docker.DockerBackend` but uses
the Podman Python SDK and the rootless Podman service socket. ``podman`` is
**lazy-imported** in :meth:`__init__`.
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
    if isinstance(policy, dict):
        return "bridge"
    if policy == "none":
        return "none"
    if policy == "local-only":
        return "host"
    return "bridge"


def _default_podman_uri() -> str:
    """Compute the default rootless Podman socket URI."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return f"unix://{runtime_dir}/podman/podman.sock"
    uid = os.geteuid() if hasattr(os, "geteuid") else 1000
    return f"unix:///run/user/{uid}/podman/podman.sock"


class PodmanBackend(SandboxBackend):
    """Rootless Podman container per project."""

    mode = "podman"

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("podman") is not None

    def __init__(
        self,
        project_dir: Optional[str] = None,
        settings: Optional[ProjectSandboxSettings] = None,
    ) -> None:
        super().__init__(project_dir=project_dir, settings=settings)
        # LAZY IMPORT: keep `podman` out of the default import graph.
        try:
            import podman as podman_sdk  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "PodmanBackend requires the 'podman' SDK. Install with: "
                "pip install 'hyperagent0[podman]'"
            ) from exc
        self._podman_sdk = podman_sdk
        self._client: Any = None
        self._container: Any = None

    def _client_lazy(self) -> Any:
        if self._client is None:
            uri = os.environ.get("CONTAINER_HOST") or _default_podman_uri()
            self._client = self._podman_sdk.PodmanClient(base_url=uri)
        return self._client

    def _bind_mounts(self) -> List[Dict[str, str]]:
        if not self.project_dir:
            return []
        host_project = path_translate.to_host(self.project_dir)
        mounts: List[Dict[str, str]] = [
            {"type": "bind", "source": host_project, "target": "/workspace"},
        ]
        knowledge = os.path.join(self.project_dir, ".a0proj", "knowledge")
        if os.path.isdir(knowledge):
            host_knowledge = path_translate.to_host(knowledge)
            mounts.append(
                {
                    "type": "bind",
                    "source": host_knowledge,
                    "target": "/workspace/.a0proj/knowledge",
                    "read_only": "true",
                }
            )
        return mounts

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
            "mounts": self._bind_mounts(),
            "network_mode": _network_mode_for(settings.network),
            "remove": not settings.persist_sandbox,
        }
        if limits.memory:
            kwargs["mem_limit"] = limits.memory
        if limits.cpus is not None and limits.cpus > 0:
            kwargs["cpu_quota"] = int(limits.cpus * 100000)
            kwargs["cpu_period"] = 100000
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

    async def open_shell(self, cwd: str) -> Any:
        container = self._start_container()
        return _PodmanExecSession(container=container, cwd=cwd)

    async def close(self) -> None:
        if self._container is not None:
            try:
                self._container.stop(timeout=2)
            except Exception:  # pragma: no cover — best-effort cleanup
                logger.debug("error stopping container", exc_info=True)
            finally:
                self._container = None


class _PodmanExecSession:
    """Minimal interactive session over Podman exec. See :class:`docker._DockerExecSession`."""

    def __init__(self, container: Any, cwd: str) -> None:
        self._container = container
        self.cwd = cwd
        self._last_output = ""

    async def connect(self) -> None:  # pragma: no cover — placeholder
        return None

    async def close(self) -> None:  # pragma: no cover — placeholder
        return None

    async def send_command(self, command: str) -> None:  # pragma: no cover
        exec_result = self._container.exec_run(
            command=["bash", "-lc", command],
            workdir=self.cwd,
            tty=True,
        )
        output = exec_result[1] if isinstance(exec_result, (tuple, list)) else exec_result
        self._last_output = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)

    async def read_output(self, timeout: float = 0, reset_full_output: bool = False):  # pragma: no cover
        out = self._last_output
        if reset_full_output:
            self._last_output = ""
        return out, None


register_backend("podman", PodmanBackend)


__all__ = ["PodmanBackend"]
