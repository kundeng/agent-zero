---
spec_id: 05-project-isolation
status: DRAFT
since: 2026-05-20
until: null
epic: architecture
features: [per-project-sandbox, cgroup-backend, docker-sandbox-backend, resource-limits, network-isolation, filesystem-isolation]
supersedes: []
superseded_by: null
depends_on: [01-host-first]
---

# Project Isolation & Sandboxing

## Context

Agent Zero has a project model (`.a0proj/`), but projects are **logically** separated (different prompts, knowledge, secrets) not **physically** isolated. All projects share:

- The same filesystem (agents can read/write across project boundaries)
- The same code execution sessions (a shell opened in Project A can `cd` into Project B)
- The same network access (no per-project network policies)
- The same resource budget (no CPU/memory/time limits per project)

For a hyperagent harness managing multiple projects, this is unacceptable. A bug in one project's code execution should not crash another. A compromised agent should not exfiltrate data from other projects.

## Constraints

- cgroup v2 requires Linux with systemd (Ubuntu 22.04+, Fedora 31+)
- macOS has no cgroups — must fall back to Docker/Podman
- No root required: use `systemd-run --user` and `unshare --map-root-user`
- Per-project config in existing `.a0proj/project.json` (extend, don't replace)
- Sandbox state ephemeral by default across sessions (configurable to persist)

## Decisions

### D1: cgroup v2 as primary backend on Linux
**Choice**: `systemd-run --user --scope` for resource limits + `unshare --mount` for filesystem isolation.
**Why**: Near-instant startup (<50ms vs 1-3s Docker). No daemon dependency. No root needed. Available on all modern Linux distros.

### D2: Per-project, not per-execution
**Choice**: Sandbox created when project activates, destroyed on deactivation or idle timeout. Persists across agent turns within a session.
**Why**: `pip install` in one turn should be available in the next turn of the same session. Per-execution sandboxes would lose this state.

### D3: Auto-detect best backend
**Choice**: On startup, probe for available backends: cgroup v2 → Docker → Podman → none.
**Why**: User shouldn't need to configure this. Works out of the box on their system.

### D4: Resource limits in project.json
**Choice**: Extend `.a0proj/project.json` with `isolation` section (sandbox_mode, resource_limits, network).
**Why**: Per-project config lives where project config already lives. No new config files.

## Tasks

### P1 — Must Do
- [ ] 1.1 Create `python/helpers/sandbox_manager.py`
  - Abstract `SandboxBackend` ABC: create/destroy/execute/is_alive
  - `NoneSandbox` implementation (passthrough, subprocess.run)
  - Backend registry and auto-detection
- [ ] 1.2 Implement `CgroupSandbox` backend (`python/helpers/sandbox_cgroup.py`)
  - Use `systemd-run --user --scope` for memory/CPU limits
  - Use `unshare --mount` for mount namespace (project dir only)
  - Session management: sandbox stays alive between execute() calls
  - Resource limits: `MemoryMax`, `CPUQuota`
- [ ] 1.3 Implement `DockerSandbox` backend (`python/helpers/sandbox_docker.py`)
  - Lightweight container per project (reuse from spec 01)
  - Bind-mount project dir RW, knowledge RO
  - `mem_limit`, `cpus`, `network_mode` from project config
- [ ] 1.4 Extend `projects.py` with isolation config
  - Add `isolation` section to project.json schema
  - Fields: `sandbox_mode`, `resource_limits` (cpus, memory, timeout, disk_quota), `network`, `persist_sandbox`
  - [src:python/helpers/projects.py]
- [ ] 1.5 Modify code execution tool to route through sandbox_manager
  - If active project has `isolation.sandbox_mode != "none"`, use sandbox
  - Otherwise fall through to existing Local/SSH session
  - [src:python/tools/code_execution_tool.py]
- [ ] 1.6 Auto-detect best available backend
  - Check: systemd + cgroup v2 available? Docker daemon running? Podman available?
  - Set project default based on detection
  - Log detected backend on startup

### P2 — Should Do
- [ ] 2.1 Implement `PodmanSandbox` backend (`python/helpers/sandbox_podman.py`)
  - Rootless Podman — same interface as Docker, no daemon
- [ ] 2.2 Network isolation modes
  - `internet` (default): full network access
  - `local-only`: can reach localhost services only
  - `none`: fully airgapped
  - Docker: `--network=host` / `--network=none` / custom bridge
  - cgroup: `unshare --net` with optional veth pair
- [ ] 2.3 Test: cgroup isolation on Ubuntu 22.04+ (memory limit enforced, OOM kill works)
- [ ] 2.4 Test: Docker isolation on macOS
- [ ] 2.5 Test: Filesystem isolation (cannot escape project directory)
- [ ] 2.6 Test: Resource limits enforced (CPU throttle, timeout kill)
- [ ] 2.7 Test: Backward compat — `sandbox_mode=none` matches current behavior exactly

### P3 — Nice to Have
- [ ] 3.1 Disk quota enforcement (cgroup: cgroupv2 io controller, Docker: `--storage-opt`)
- [ ] 3.2 Sandbox status in web UI (resource usage, uptime, network mode)
- [ ] 3.3 Sandbox snapshot/restore (save installed packages, restore on next session)

## Open Questions

- [ ] Should sandbox_mode be settable globally AND per-project, with per-project overriding? Yes, likely.
- [ ] How to handle `pip install` across sessions when sandbox is ephemeral? Document clearly. Offer `persist_sandbox: true` option.
- [ ] cgroup v2 `unshare --mount` may need user namespace support. Test on major distros.

## Log

**2026-05-20** — Initial spec. Confirmed Agent Zero's project model at `python/helpers/projects.py` — projects are `usr/projects/<name>/.a0proj/`. Code execution is in `python/tools/code_execution_tool.py` with `LocalInteractiveSession` (host PTY) and `SSHInteractiveSession` (Docker SSH). The sandbox_manager bridges between these.
