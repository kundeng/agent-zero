---
spec_id: 01-host-first
status: DRAFT
since: 2026-05-20
until: null
epic: architecture
features: [host-native-install, host-mode-defaults, sandbox-mode-setting, docker-fallback]
supersedes: []
superseded_by: null
depends_on: []
---

# Host-First Architecture

## Context

Agent Zero assumes it runs inside a Docker container. The Dockerfile, installer, docs, and default config all point to a containerized deployment. This is architecturally wrong for a hyperagent harness:

- The **agent** needs host access: filesystem, network, process management, credential stores
- Only **code execution** (untrusted LLM-generated code) should be sandboxed
- Running the agent in Docker prevents it from managing other containers, accessing host tools, or composing with host services

The code already supports host-mode (`ssh_enabled=false`, `rfc_auto_docker=false`, `LocalInteractiveSession`), but it's undocumented, untested as the primary path, and the startup sequence assumes Docker.

## Constraints

- Must not break existing Docker deployment path (backward compat via `DEPLOYMENT_MODE=docker`)
- Python 3.11+ required
- No new mandatory system dependencies beyond Python/pip
- Docker/Podman optional — only needed if user wants sandboxed code execution

## Decisions

### D1: DEPLOYMENT_MODE env var
**Choice**: `DEPLOYMENT_MODE=host` (default) vs `DEPLOYMENT_MODE=docker` (legacy). Auto-detected if running inside Docker container.
**Why**: Clean separation. Existing Docker users are auto-detected and get unchanged behavior. New users get host-first by default.

### D2: sandbox_mode setting
**Choice**: New setting `sandbox_mode`: `none` | `docker` | `podman` | `cgroup`. Default: `none`.
**Why**: Decouples "where the agent runs" from "where code executes". Agent always runs on host; code execution isolation is independently configurable.

### D3: Lightweight sandbox image
**Choice**: When `sandbox_mode=docker`, use a slim image (python:3.11-slim + git/curl/jq), NOT the full Agent Zero base image.
**Why**: The sandbox only needs a code execution environment. No Flask, no webui, no agent code. Fast to build, small to pull.

### D4: pyproject.toml for pip install
**Choice**: Package HyperAgent Zero as a pip-installable package with entry points.
**Why**: `pip install hyperagent-zero && hyperagent-zero start` is the simplest onboarding path. Entry points provide the CLI without manual PATH management.

## Tasks

### P1 — Must Do
- [ ] 1.1 Create `pyproject.toml` with proper entry points and metadata
  - Package name `hyperagent-zero`, entry point `hyperagent-zero = "cli:main"`
  - Pin existing requirements.txt deps
- [ ] 1.2 Refactor `runtime.py` to support both modes cleanly
  - `is_host_mode()` / `is_docker_mode()` replacing `is_dockerized()`
  - Read `DEPLOYMENT_MODE` env var, auto-detect if inside Docker
  - [src:python/helpers/runtime.py]
- [ ] 1.3 Update `settings.py` defaults for host mode
  - `ssh_enabled=false`, `rfc_auto_docker=false`, `rfc_url=""` when host mode
  - New `sandbox_mode` field (default: `none`)
  - [src:python/helpers/settings.py]
- [ ] 1.4 Create `python/helpers/sandbox_manager.py`
  - Abstract `SandboxBackend` with create/destroy/execute
  - `NoneSandbox` (passthrough subprocess), `DockerSandbox`, `PodmanSandbox` stubs
  - Backend auto-detection: check what's available on system
- [ ] 1.5 Modify code execution tool to use sandbox manager
  - When project has sandbox_mode != none, route through sandbox_manager
  - Otherwise use existing LocalInteractiveSession / SSHInteractiveSession
  - [src:python/tools/code_execution_tool.py]
- [ ] 1.6 Create lightweight sandbox Dockerfile
  - `docker/sandbox/Dockerfile` — python:3.11-slim + git curl jq
  - Project dir mounted RW, shared tmp dir

### P2 — Should Do
- [ ] 2.1 Write host-mode installation docs
  - Update `docs/setup/installation.md` with pip install path
- [ ] 2.2 Test: host mode startup without Docker installed
- [ ] 2.3 Test: host mode with Docker sandbox for code execution
- [ ] 2.4 Test: backward compat — Docker mode unchanged when DEPLOYMENT_MODE=docker

### P3 — Nice to Have
- [ ] 3.1 Auto-detect optimal sandbox backend on first run
  - Check: Docker available? Podman available? cgroup v2 + systemd? Fall back to none.

## Open Questions

- [ ] Should `sandbox_mode` be global or per-project? (Likely per-project — see spec 05-project-isolation)
- [x] Can we flip defaults without breaking Docker users? Yes — auto-detect `DEPLOYMENT_MODE` from container env.

## Log

**2026-05-20** — Initial spec drafted from codebase review. Confirmed `ssh_enabled`, `rfc_auto_docker`, `LocalInteractiveSession` already exist in upstream code. Key files: `runtime.py`, `settings.py`, `code_execution_tool.py`.
