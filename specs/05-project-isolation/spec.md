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

### Relationship to spec 01

Spec 01-host-first established the `sandbox_mode` taxonomy by **process relationship to the agent** and shipped three modes:

| Mode | Process relationship | Owner |
|------|---------------------|-------|
| `none` | local subprocess, no wrapper | spec 01 |
| `sandbox` | local subprocess with OS-level FS/network restrictions (srt) | spec 01 |
| `ssh` | remote process | spec 01 |

This spec extends the taxonomy with the **isolated-process-tree** modes:

| Mode | Process relationship | Owner |
|------|---------------------|-------|
| `cgroup` | local subprocess with cgroup v2 resource limits + mount namespace | spec 05 |
| `docker` | fresh container — separate process tree, isolated kernel view | spec 05 |
| `podman` | fresh container via Podman — separate process tree, rootless | spec 05 |

Spec 01 already created the `hyperagent0/sandbox/` package with the `SandboxBackend` ABC, `NoneBackend`, `SandboxBackendSrt`, `SshBackend`, the registry (`get_backend`, `register_backend`), and the per-project `sandbox` block in `project.json` (with mode literal `inherit | none | sandbox | ssh`). This spec **extends** that infrastructure — it does not recreate it.

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

> **Prerequisite from spec 01:** `hyperagent0/sandbox/` already exists with the `SandboxBackend` ABC, the `register_backend()` / `get_backend()` registry, `NoneBackend`, `SandboxBackendSrt`, and `SshBackend`. `project.json` already has a `sandbox` block. `code_execution_tool.py` already routes through the registry. The tasks below **register new backends and broaden the schema**, not rebuild.

- [ ] 1.1 Broaden mode literal and per-project schema
  - Extend `ProjectSandboxSettings` mode literal to `inherit | none | sandbox | ssh | cgroup | docker | podman`
  - Add fields: `resource_limits` (cpus, memory, timeout, disk_quota), `network` (internet | local-only | none | allowlist), `image`, `persist_sandbox`
  - Existing `resolve_sandbox_mode()` call sites unchanged
  - [src:python/helpers/projects.py, python/helpers/settings.py]
- [ ] 1.2 Implement `CgroupBackend` (`hyperagent0/sandbox/cgroup.py`)
  - Register with the existing `sandbox_manager` registry for `sandbox_mode=cgroup`
  - Use `systemd-run --user --scope` for memory/CPU limits
  - Use `unshare --mount` for mount namespace (project dir only)
  - Session management: sandbox stays alive between execute() calls
  - Resource limits: `MemoryMax`, `CPUQuota`
  - Process relationship: local subprocess (sibling of `sandbox` but kernel-enforced rather than userspace)
- [ ] 1.3 Implement `DockerBackend` (`hyperagent0/sandbox/docker.py`)
  - Register with the existing `sandbox_manager` registry for `sandbox_mode=docker`
  - Spawns a **fresh** container per project (never reuses the agent's container, even in docker-in-docker)
  - Image from task 1.7 below; bind-mount project dir RW (via `path_translate` from task 1.6), knowledge RO
  - `mem_limit`, `cpus`, `network_mode` from project config
  - Lazy import: `import docker` happens only when this backend is constructed
- [ ] 1.4 Implement `PodmanBackend` (`hyperagent0/sandbox/podman.py`)
  - Rootless containers via Podman; same interface as `DockerBackend`
  - Register for `sandbox_mode=podman`
- [ ] 1.5 Auto-detect best available backend
  - Order: `sandbox` (srt, from spec 01) → `cgroup` → `docker` → `podman` → `none`
  - Setup wizard suggests the detected mode
  - Log detected mode on startup
- [ ] 1.6 Create `hyperagent0/sandbox/path_translate.py` (moved from spec 01)
  - `to_host(path) -> str`: identity on host mode; in docker mode reads `/proc/self/mountinfo` to map agent-internal paths to host paths
  - `from_host(path) -> str`: inverse, for surfacing sandbox results back to the agent
  - Used by `DockerBackend`/`PodmanBackend` volume mounts; not needed by `none`/`sandbox`/`ssh`/`cgroup`
  - Unit tests with synthetic mountinfo fixtures (no Docker required)
- [ ] 1.7 Create lightweight sandbox Dockerfile (moved from spec 01)
  - `docker/sandbox/Dockerfile` — python:3.11-slim + git curl jq
  - Project dir mounted RW, shared tmp dir
  - Repo-root `docker/` directory is acceptable; not part of any Python package
- [ ] 1.8 Add Python extras for container SDKs
  - `pyproject.toml` extras: `[docker] = ["docker>=7"]`, `[podman] = ["podman>=4"]`
  - Lazy imports in the respective backends — install error surfaces only when that mode is selected
  - [src:pyproject.toml]

> **Conflict-surface budget for spec 05**: zero upstream patches in `python/`. All new backends live in `hyperagent0/sandbox/`; the schema extension to `project.json` is via spec 01's `BasicProjectData.sandbox` block (broadened literal only — no new struct).

- [ ] 2.1 (removed — Podman backend promoted to P1 task 1.4)
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

- [x] Should sandbox_mode be settable globally AND per-project? **Yes** — resolved by spec 01 D5. Global default in `settings.json`, per-project override in `project.json#sandbox`, `inherit` as default project value.
- [ ] How to handle `pip install` across sessions when sandbox is ephemeral? Document clearly. Offer `persist_sandbox: true` option.
- [ ] cgroup v2 `unshare --mount` may need user namespace support. Test on major distros.
- [ ] Should `cgroup` and `sandbox` (srt) collapse into a single `sandbox` mode with a `backend` sub-field (`srt | bwrap | cgroup`)? Both are local subprocesses with kernel/userspace restrictions — same process relationship to the agent. Argument for: cleaner taxonomy. Argument against: cgroup adds resource limits that srt doesn't, and users likely think of them differently. Defer until both backends ship and we see how config files read.

## Log

**2026-05-20** — Initial spec. Confirmed Agent Zero's project model at `python/helpers/projects.py` — projects are `usr/projects/<name>/.a0proj/`. Code execution is in `python/tools/code_execution_tool.py` with `LocalInteractiveSession` (host PTY) and `SSHInteractiveSession` (Docker SSH). The sandbox_manager bridges between these.

**2026-05-20** — Aligned with spec 01 revisions. Spec 01 now ships `sandbox_manager.py` with the ABC, `NoneBackend`, `SandboxBackendSrt` (for `sandbox_mode=sandbox`), and `SshBackend`; this spec extends with `CgroupBackend`, `DockerBackend`, `PodmanBackend`. Tasks restructured: 1.1 broadens the schema rather than recreating it, 1.6 picks up `path_translate` and 1.7 picks up the sandbox Dockerfile (both moved from spec 01 because they're only consumed by container modes). Updated naming to match spec 01's process-relationship taxonomy (`Backend` suffix for ABC implementations, mode names align with the rename `srt → sandbox`). Surfaced one new open question: whether `cgroup` and `sandbox` should collapse into a single mode with a `backend` sub-field.

**2026-05-20** — Updated all backend paths from `python/helpers/sandbox_*.py` to `hyperagent0/sandbox/*.py` per spec 01 D9 wrapper architecture. Documented the spec-05 conflict-surface budget: zero upstream patches in `python/`. All new backends are additive in `hyperagent0/sandbox/`.
