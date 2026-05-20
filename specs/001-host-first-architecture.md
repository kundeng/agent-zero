---
title: "Host-First Architecture"
status: draft
priority: P0
breaks_compat: true
depends_on: []
---

# Spec 001: Host-First Architecture

## Problem

Agent Zero assumes it runs inside a Docker container. The Dockerfile, installer, docs, and default config all point to a containerized deployment. This is architecturally wrong for a hyperagent harness:

- The **agent** needs host access: filesystem, network, process management, credential stores
- Only **code execution** (untrusted LLM-generated code) should be sandboxed
- Running the agent in Docker prevents it from managing other containers, accessing host tools, or composing with host services

The code already supports host-mode (`ssh_enabled=false`, `rfc_auto_docker=false`, `LocalInteractiveSession`), but it's undocumented, untested as the primary path, and the startup sequence assumes Docker.

## Requirements

### R1: Host-native installation as the default path
- `pip install hyperagent-zero` or `pipx install hyperagent-zero` works on Linux/macOS
- No Docker required for the agent process itself
- Dependencies: Python 3.11+, pip, optional Docker/Podman for sandboxing

### R2: Config defaults assume host mode
- `ssh_enabled` defaults to `false`
- `rfc_auto_docker` defaults to `false`  
- `rfc_url` defaults to `""` (empty = local execution)
- Code execution uses `LocalInteractiveSession` by default

### R3: Docker becomes opt-in for code execution sandboxing
- When Docker/Podman is available, code execution CAN be routed to a container
- Controlled by a new `sandbox_mode` setting: `none` | `docker` | `podman` | `cgroup`
- Default: `none` (code runs in a subprocess on host, same as `LocalInteractiveSession`)
- When `sandbox_mode=docker`: spawn a lightweight sandbox container per code execution, not a full Agent Zero image

### R4: Preserve upstream Docker mode as a fallback
- The existing `DockerfileLocal` and `docker/` tree remain functional
- Users who WANT the all-in-Docker experience can still use it
- A `DEPLOYMENT_MODE` env var (`host` | `docker`) switches between the two paths
- Default: `host`

## Design

### Current startup flow (Docker)
```
DockerfileLocal → supervisord → run_ui.py → Flask+SocketIO → agent loop
```

### New startup flow (Host)
```
hyperagent-zero start → run_ui.py → Flask+SocketIO → agent loop
                                   ↳ sandbox manager (optional Docker/cgroup for code exec)
```

### Key files to modify

| File | Change |
|------|--------|
| `initialize.py` | Read `DEPLOYMENT_MODE`, set defaults accordingly |
| `python/helpers/settings.py` | New defaults for host mode, new `sandbox_mode` field |
| `python/helpers/runtime.py` | `is_dockerized()` becomes `is_host_mode()` / `is_docker_mode()` |
| `plugins/_code_execution/tools/code_execution_tool.py` | Route to sandbox manager based on `sandbox_mode` |
| `plugins/_code_execution/helpers/shell_local.py` | Add optional cgroup wrapping |
| NEW: `python/helpers/sandbox_manager.py` | Manage sandbox lifecycle (spawn, mount, destroy) |
| NEW: `setup.py` / `pyproject.toml` | Package for pip install |

### Sandbox container (when sandbox_mode=docker)

Lightweight image (not the full Agent Zero base):
```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y git curl jq
# No Flask, no webui, no agent code — just a code execution environment
COPY sandbox_entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
```

Mount only the project working directory (read-write) and a shared tmp dir. No host filesystem access beyond that.

## Migration

Existing Docker users:
- Set `DEPLOYMENT_MODE=docker` to preserve current behavior (auto-detected if running inside Docker)
- No config changes needed

New users:
- `pip install hyperagent-zero && hyperagent-zero start` — just works on host

## Tasks

- [ ] Create `pyproject.toml` with proper entry points
- [ ] Refactor `runtime.py` to support both modes cleanly
- [ ] Update `settings.py` defaults for host mode
- [ ] Create `sandbox_manager.py` with Docker/cgroup/none backends
- [ ] Modify code execution plugin to use sandbox manager
- [ ] Create lightweight sandbox Dockerfile
- [ ] Write host-mode installation docs
- [ ] Test: host mode startup without Docker installed
- [ ] Test: host mode with Docker sandbox for code execution
- [ ] Test: backward compat Docker mode unchanged
