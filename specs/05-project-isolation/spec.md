---
spec_id: 05-project-isolation
status: WITHDRAWN
since: 2026-05-20
until: 2026-05-22
epic: architecture
features: []
supersedes: []
superseded_by: null
depends_on: [01-host-first]
---

# Project Isolation & Sandboxing — WITHDRAWN 2026-05-22

> **This spec is withdrawn.** Sandbox mode is now a single global setting
> (`none` / `sandbox` / `ssh`) inherited from the agent's deployment
> environment. There is no per-project override, no cgroup/docker/podman
> backend, and no network-allowlist enforcement.
>
> **Rationale** (user, 2026-05-22): "If the agent runs on the host, code
> execution also runs with the sandbox. If the agent already runs in a
> Docker container, then again it's just a sandbox. I think that's good
> enough — scratch this project-level isolation altogether."
>
> **What was removed**: `hyperagent0/sandbox/{cgroup,docker,podman,path_translate}.py`,
> the `ProjectSandboxSettings` TypedDict in `python/helpers/projects.py`,
> the `sandbox` block in `project.json`, the `hyperagent0/projects/`
> subpackage (`resolve_sandbox_mode` / `set_agent_sandbox_mode`),
> `pyproject.toml` `[docker]` / `[podman]` extras, `docker/sandbox/Dockerfile`,
> and `tests/test_{sandbox_registry,path_translate,hyperagent0_project_sandbox}.py`.
>
> **What spec 06 lost**: D5 (channel↔sandbox bridge), `ChannelConfig.sandbox_override`,
> `ChannelRouter._apply_sandbox_override`, and the three associated tests.
>
> The contents below are preserved for historical context only.

---

# (Historical) Project Isolation & Sandboxing

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

- [x] 1.1 Broaden mode literal and per-project schema
  - Extended `ProjectSandboxSettings` mode literal to `inherit | none | sandbox | ssh | cgroup | docker | podman`
  - Fields shipped: `resource_limits` (cpus, memory, timeout, disk_quota), `network` (internet | local-only | none | allowlist dict), `image`, `persist_sandbox`
  - Runtime dataclass lives in `hyperagent0/sandbox/__init__.py`; JSON-on-disk TypedDict stays in `python/helpers/projects.py`
  - [src:hyperagent0/sandbox/__init__.py:160-206, python/helpers/projects.py]
- [x] 1.2 Implement `CgroupBackend` (`hyperagent0/sandbox/cgroup.py`)
  - Registered for `sandbox_mode=cgroup`; lazy import; `is_available()` probes `systemd-run` + `unshare` + cgroup-v2 mount
  - `build_wrapper_argv()` constructs the full `systemd-run --user --scope -p MemoryMax=… -p CPUQuota=… unshare --mount --map-root-user [--net]` argv
  - **Residual (tracked as Open Question)**: `open_shell()` currently stashes the wrapped argv on the `LocalInteractiveSession` but does not yet make the upstream PTY exec through it. Full PTY-through-wrapper integration deferred until `LocalInteractiveSession` accepts an explicit `terminal` argument (cross-cutting with spec 01)
- [x] 1.3 Implement `DockerBackend` (`hyperagent0/sandbox/docker.py`)
  - Registered for `sandbox_mode=docker`; lazy-imports `docker` SDK with friendly install hint
  - Container lifecycle complete: bind-mount project RW + knowledge RO, `mem_limit` / `nano_cpus` / `network_mode` from settings, `auto_remove` honors `persist_sandbox`
  - **Residual (tracked as Open Question)**: `_DockerExecSession.send_command/read_output` are minimal `exec_run` wrappers — no streaming, no PTY, no timeouts. Adequate for smoke testing; not production-grade
- [x] 1.4 Implement `PodmanBackend` (`hyperagent0/sandbox/podman.py`)
  - Rootless containers via Podman service socket; same shape as Docker backend
  - Shares the same exec-streaming residual as Docker (tracked under task 1.3)
- [x] 1.5 Auto-detect best available backend
  - Probe order shipped: `sandbox` (srt) → `cgroup` → `docker` → `podman` → `none`
  - `recommend_mode_for_wizard()` returns `(mode, hint)` for the setup UI
  - [src:hyperagent0/sandbox/__init__.py:214-266]
- [x] 1.6 Create `hyperagent0/sandbox/path_translate.py`
  - 210 lines; reads `/proc/self/mountinfo`, `to_host()` / `from_host()` round-trip, unit-tested with synthetic fixtures (no Docker required)
- [x] 1.7 Create lightweight sandbox Dockerfile
  - `docker/sandbox/Dockerfile` shipped at repo root
- [x] 1.8 Add Python extras for container SDKs
  - `pyproject.toml` has `[docker] = ["docker>=7"]`, `[podman] = ["podman>=4"]`
  - Lazy imports verified — `import hyperagent0.sandbox` does not pull either SDK

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
- [ ] **Backend exec-streaming closeout (residual from P1 1.2/1.3/1.4).** The cgroup/docker/podman backends ship with placeholder execution paths: cgroup attaches the wrapper argv to `LocalInteractiveSession` but doesn't drive its PTY through it; `_DockerExecSession` / `_PodmanExecSession` use a synchronous `exec_run` wrapper with no streaming, PTY, or per-command timeout. For unsandboxed dev use this is fine — but `sandbox_mode=docker` looks like it isolates and only partially does. Two paths forward: (a) finish: take a `terminal` argument on `LocalInteractiveSession` upstream + replace exec wrappers with a `start_exec` + socket-stream loop in our backends; (b) gate: refuse `sandbox_mode∈{cgroup,docker,podman}` from `code_execution_tool.py` unless an explicit `allow_experimental_sandbox=true` flag is set. Pick before either backend is recommended to users.

## Log

**2026-05-20** — Initial spec. Confirmed Agent Zero's project model at `python/helpers/projects.py` — projects are `usr/projects/<name>/.a0proj/`. Code execution is in `python/tools/code_execution_tool.py` with `LocalInteractiveSession` (host PTY) and `SSHInteractiveSession` (Docker SSH). The sandbox_manager bridges between these.

**2026-05-20** — Aligned with spec 01 revisions. Spec 01 now ships `sandbox_manager.py` with the ABC, `NoneBackend`, `SandboxBackendSrt` (for `sandbox_mode=sandbox`), and `SshBackend`; this spec extends with `CgroupBackend`, `DockerBackend`, `PodmanBackend`. Tasks restructured: 1.1 broadens the schema rather than recreating it, 1.6 picks up `path_translate` and 1.7 picks up the sandbox Dockerfile (both moved from spec 01 because they're only consumed by container modes). Updated naming to match spec 01's process-relationship taxonomy (`Backend` suffix for ABC implementations, mode names align with the rename `srt → sandbox`). Surfaced one new open question: whether `cgroup` and `sandbox` should collapse into a single mode with a `backend` sub-field.

**2026-05-20** — Updated all backend paths from `python/helpers/sandbox_*.py` to `hyperagent0/sandbox/*.py` per spec 01 D9 wrapper architecture. Documented the spec-05 conflict-surface budget: zero upstream patches in `python/`. All new backends are additive in `hyperagent0/sandbox/`.

**2026-05-22** — Audit pass against committed code. **All P1 (1.1–1.8) shipped** as classes registered in the spec-01 backend registry, with lazy SDK imports, full `is_available()` probes, and unit tests in `tests/test_hyperagent0_sandbox_registry.py` + `tests/test_hyperagent0_project_sandbox.py` (17 passed, 11 environment-conditional). However, the cgroup/docker/podman backends ship with **placeholder execution paths** — see new Open Question on exec-streaming closeout. Foundation is solid (schema, registry, auto-detect, path_translate, Dockerfile, extras) but `sandbox_mode∈{cgroup,docker,podman}` is not yet safe to recommend to users. P2 mostly pending (real-hardware integration tests + iptables/nftables network enforcement for the allowlist policy).

**2026-05-22 (later)** — **WITHDRAWN.** User reviewed the audit findings and the placeholder exec-streaming question and chose to scrap project-level isolation entirely rather than finish or gate the half-implementation. Sandbox mode collapses to one global setting per host. The removal touched 4 backend files + path_translate, the `hyperagent0/projects/` subpackage, the `ProjectSandboxSettings` TypedDict and `sandbox` block in `project.json`, two pyproject extras, the Dockerfile, three test files, and spec 06 D5. The `code_execution_tool._resolve_sandbox_mode_with_legacy` resolver simplified to read `Settings.sandbox_mode` directly (no more `AgentConfig.additional` plumbing). All 57 channel+sandbox+haz tests pass after the cut.
