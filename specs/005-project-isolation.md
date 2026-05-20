---
title: "Project Isolation & Sandboxing"
status: draft
priority: P1
breaks_compat: true
depends_on: ["001-host-first-architecture"]
---

# Spec 005: Project Isolation & Sandboxing

## Problem

Agent Zero has a project model (`.a0proj/`), but projects are **logically** separated (different prompts, knowledge, secrets) not **physically** isolated. All projects share:

- The same filesystem (agents can read/write across project boundaries)
- The same code execution sessions (a shell opened in Project A can `cd` into Project B)
- The same network access (no per-project network policies)
- The same resource budget (no CPU/memory/time limits per project)

For a hyperagent harness managing multiple projects, this is unacceptable. A bug in one project's code execution should not crash another project. A compromised agent should not be able to exfiltrate data from other projects.

## Requirements

### R1: Per-project execution sandbox
- Each project's code execution runs in its own isolated environment
- Isolation level configurable per project: `none` | `cgroup` | `docker` | `podman`
- Default: `cgroup` on Linux (lightweight, no Docker needed), `docker` on macOS

### R2: Filesystem isolation
- Each sandbox can only see its project's working directory (`usr/projects/<name>/`)
- Shared read-only mounts: agent skills, knowledge base
- No cross-project filesystem access from within a sandbox
- Host agent process retains full filesystem access (it needs to manage projects)

### R3: Resource limits
- Per-project configurable: CPU cores, memory limit, execution timeout, disk quota
- Defaults: 2 cores, 2GB RAM, 5min timeout, 1GB disk
- Stored in `.a0proj/project.json` alongside existing project config

### R4: Network isolation
- Default: sandbox has internet access (needed for pip install, git clone, etc.)
- Configurable per-project: `internet` | `local-only` | `none`
- `local-only`: can reach localhost services (databases, APIs) but not external internet
- `none`: fully airgapped

### R5: Sandbox lifecycle tied to project
- Sandbox created when project is activated
- Sandbox destroyed when project is deactivated or after idle timeout
- Sandbox state (installed packages, built artifacts) persists across agent turns within a session
- Sandbox state is ephemeral across sessions by default (configurable to persist)

## Design

### Isolation backends

```python
# python/helpers/sandbox_manager.py

class SandboxBackend(ABC):
    @abstractmethod
    async def create(self, project: ProjectConfig) -> Sandbox: ...
    
    @abstractmethod
    async def destroy(self, sandbox_id: str) -> None: ...
    
    @abstractmethod
    async def execute(self, sandbox_id: str, command: str, timeout: int) -> ExecResult: ...

class NoneSandbox(SandboxBackend):
    """No isolation. subprocess.run() directly."""
    ...

class CgroupSandbox(SandboxBackend):
    """Linux cgroup v2 isolation. No Docker needed.
    Uses systemd-run --user for cgroup scoping.
    Mount namespace via unshare for filesystem isolation."""
    ...

class DockerSandbox(SandboxBackend):
    """Docker container per project.
    Lightweight image, project dir mounted RW."""
    ...

class PodmanSandbox(SandboxBackend):
    """Rootless Podman. Same as Docker but no daemon, no root."""
    ...
```

### cgroup backend (Linux, preferred)

Using `systemd-run --user` for cgroup scoping (no root needed):

```bash
# Create a transient scope for project "my-project"
systemd-run --user --scope \
    --property=MemoryMax=2G \
    --property=CPUQuota=200% \
    --unit=hz-project-my-project \
    -- unshare --mount --map-root-user \
    bash -c 'mount --bind /path/to/project /workspace && cd /workspace && exec bash'
```

This gives:
- Memory limit (2GB)
- CPU limit (2 cores = 200%)
- Mount namespace isolation (project dir only)
- No Docker dependency
- No root privilege needed (systemd --user + unshare --map-root-user)

### Docker backend (macOS, or when Docker is preferred)

```python
async def create(self, project: ProjectConfig) -> Sandbox:
    container = docker.run(
        image="hyperagent-zero-sandbox:latest",
        mounts=[
            Mount(project.work_dir, "/workspace", type="bind"),
            Mount(project.a0proj_dir / "knowledge", "/knowledge", type="bind", read_only=True),
        ],
        mem_limit=project.resource_limits.memory,
        cpus=project.resource_limits.cpus,
        network_mode=self._network_mode(project.network_policy),
        detach=True,
        remove=False,  # persist across executions
    )
    return DockerSandboxHandle(container.id)
```

### Project config extension

```json
// usr/projects/my-project/.a0proj/project.json
{
    "title": "My Project",
    "description": "...",
    "isolation": {
        "sandbox_mode": "cgroup",
        "resource_limits": {
            "cpus": 2,
            "memory": "2G",
            "timeout": 300,
            "disk_quota": "1G"
        },
        "network": "internet",
        "persist_sandbox": false
    }
}
```

### Integration with code execution plugin

The existing `code_execution_tool.py` dispatches to either `SSHInteractiveSession` or `LocalInteractiveSession`. We add a third path:

```python
# In code_execution_tool.py
if project and project.isolation.sandbox_mode != "none":
    session = sandbox_manager.get_session(project.name)
    result = await session.execute(code, timeout=project.isolation.resource_limits.timeout)
else:
    # Existing local/SSH path
    session = self._get_shell_session(...)
    result = await session.execute(code)
```

### Key files

| File | Purpose |
|------|---------|
| NEW: `python/helpers/sandbox_manager.py` | Backend abstraction, sandbox lifecycle |
| NEW: `python/helpers/sandbox_cgroup.py` | cgroup v2 + unshare backend |
| NEW: `python/helpers/sandbox_docker.py` | Docker backend |
| NEW: `python/helpers/sandbox_podman.py` | Podman backend |
| `python/helpers/projects.py` | Add `isolation` config to project model |
| `plugins/_code_execution/tools/code_execution_tool.py` | Route to sandbox based on project config |
| NEW: `docker/sandbox/Dockerfile` | Lightweight sandbox image |

## Risks

- **cgroup v2 availability**: Requires systemd with cgroup v2. Most modern Linux distros have this (Ubuntu 22.04+, Fedora 31+). Fallback to Docker if unavailable.
- **unshare permissions**: `unshare --mount` may require `CAP_SYS_ADMIN` or user namespace support. Test on major distros.
- **State management**: If sandbox persists across turns but not sessions, users may be confused when `pip install` works within a session but packages are gone next session. Clear messaging needed.
- **Performance**: Docker container startup adds ~1-3s latency. cgroup scope is near-instant. Prefer cgroup on Linux.

## Tasks

- [ ] Create `sandbox_manager.py` with backend abstraction
- [ ] Implement `NoneSandbox` (passthrough, no isolation)
- [ ] Implement `CgroupSandbox` (systemd-run + unshare)
- [ ] Implement `DockerSandbox` (lightweight container)
- [ ] Implement `PodmanSandbox` (rootless container)
- [ ] Extend `projects.py` with isolation config in project.json
- [ ] Modify code_execution_tool.py to route through sandbox_manager
- [ ] Create lightweight sandbox Docker image
- [ ] Auto-detect best available backend (cgroup → docker → podman → none)
- [ ] Test: cgroup isolation on Ubuntu 22.04+
- [ ] Test: Docker isolation on macOS
- [ ] Test: Resource limits enforced (OOM kill, CPU throttle, timeout)
- [ ] Test: Filesystem isolation (cannot escape project directory)
- [ ] Test: Network isolation modes
- [ ] Test: Backward compat (sandbox_mode=none matches current behavior)
