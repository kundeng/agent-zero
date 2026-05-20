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

### Three-axis architecture

This spec separates three previously-conflated concerns. Future specs (chat channels, project isolation, daemon) plug into the same axes without re-litigating the model.

```
┌─────────────────────────────────────────────────────────────────────┐
│                  Axis 1: where the AGENT runs                        │
│                                                                      │
│           DEPLOYMENT_MODE = host (default)  |  docker (legacy)       │
│           ─────────────────────────────────────────────────────      │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │            Axis 3: who is connected to the agent              │   │
│  │            (all in-process async tasks/handlers)              │   │
│  │                                                                │   │
│  │   Web UI ─┐                                                   │   │
│  │   Telegram│                                                   │   │
│  │   Slack   ├──→ router ──→ AgentContext ──→ Agent.monologue()  │   │
│  │   Discord │                    ▲                              │   │
│  │   Cron    ┘                    │                              │   │
│  │                       (per-project bindings)                  │   │
│  │                                                                │   │
│  │                              │                                 │   │
│  │                              ▼                                 │   │
│  │                  code_execution_tool                           │   │
│  │                              │                                 │   │
│  │                              ▼                                 │   │
│  │              sandbox_manager.get_backend(mode)                 │   │
│  └────────────────────────────│─│─│─────────────────────────────┘   │
│                               │ │ │                                   │
│                               ▼ ▼ ▼                                   │
│                  Axis 2: where CODE EXECUTION runs                    │
│              sandbox_mode = none | sandbox | ssh   (this spec)        │
│                           + docker | podman | cgroup  (spec 05)       │
│              taxonomy is by PROCESS RELATIONSHIP to the agent         │
│              (global default, per-project override)                   │
└─────────────────────────────────────────────────────────────────────┘
```

**Orthogonality.** Axes 1 and 2 are independent. Any combination is valid:
- `agent=host + sandbox=none` — simplest dev setup
- `agent=host + sandbox=docker` — safest production (untrusted code in container)
- `agent=docker + sandbox=none` — legacy upstream behavior
- `agent=docker + sandbox=docker` — docker-in-docker via mounted `/var/run/docker.sock`

The only couplings between axes 1 and 2 are (a) **path translation** when the agent's view of project paths differs from the host's view, and (b) **network reachability** between agent and host services (already handled by `get_local_url()`). Both live inside `sandbox_manager` so the rest of the codebase stays oblivious.

**Axis 3 is in-process.** Channel adapters (spec 04), the scheduler (`python/helpers/task_scheduler.py`), and the web UI all run as async tasks/handlers inside the agent process. They route inbound work to an `AgentContext`, which inherits its sandbox decision from the project it activates. New entry points (channels, cron jobs) get sandboxing for free — they never touch the execution layer directly.

## Constraints

- Must not break existing Docker deployment path (backward compat via `DEPLOYMENT_MODE=docker`)
- Python 3.11+ required
- No new mandatory system dependencies beyond Python/pip
- Docker/Podman optional — only needed if user wants sandboxed code execution

## Decisions

### D1: DEPLOYMENT_MODE env var
**Choice**: `DEPLOYMENT_MODE=host` (default) vs `DEPLOYMENT_MODE=docker` (legacy). Auto-detected if running inside Docker container.
**Why**: Clean separation. Existing Docker users are auto-detected and get unchanged behavior. New users get host-first by default.

### D2: sandbox_mode setting — taxonomy by process relationship
**Choice**: New setting `sandbox_mode`. Values defined in this spec: `none` | `sandbox` | `ssh`. Default: `none`. Spec 05-project-isolation extends the literal with `docker` | `podman` | `cgroup`.

The taxonomy is by **process relationship to the agent**, not by isolation primitive:

| Mode | Spec | Process relationship | Implementation |
|------|------|----------------------|----------------|
| `none` | 01 | Local subprocess, no sandbox wrapper | `LocalInteractiveSession` (today's default) |
| `sandbox` | 01 | Local subprocess wrapped by an OS-level sandboxer (FS + network restrictions) | `srt` (Anthropic [`sandbox-runtime`](https://github.com/anthropic-experimental/sandbox-runtime)) as the initial backend; future alternatives like bare `bubblewrap` can register against the same mode |
| `ssh` | 01 | Remote process over SSH (different host or container) | `SSHInteractiveSession` (today's upstream-Docker workflow) |
| `docker` | 05 | Fresh container — separate process tree, isolated kernel view | spawned via Docker daemon (docker-in-docker via mounted `/var/run/docker.sock` when agent itself is in a container) |
| `podman` | 05 | Fresh container via Podman — separate process tree | rootless containers, no daemon |
| `cgroup` | 05 | Local subprocess with cgroup v2 resource limits + mount namespace | `systemd-run --user` + `unshare`; sibling of `sandbox` (subprocess) but enforced by kernel rather than userspace sandboxer |

**Mode semantics resolves the "agent in docker + sandbox=docker — same container?" ambiguity:** `sandbox_mode=docker` always means *spawn a fresh container*. There is no mode that means "reuse the agent's current container" except `none`, which means "share whatever environment the agent is in."

**Why**: A single dimension (process relationship) is easier to reason about than two ("isolation primitive" × "where the process lives"). The `sandbox` name reads naturally in config files (`sandbox_mode: "sandbox"`) and lets the implementation evolve without renaming the user-facing knob. `ssh` preserves the upstream Docker workflow byte-for-byte for users who already depend on it.

### D3: Lightweight sandbox image (deferred to spec 05)
**Choice**: Since `sandbox_mode=docker` is owned by spec 05, the sandbox Dockerfile and image-management logic move there.
**Why**: Spec 01 ships with `none` and `srt` only — neither needs a container image. Keeping the Dockerfile work in spec 05 colocates it with the `DockerSandbox` backend implementation.

### D4: pyproject.toml for pip install
**Choice**: Package HyperAgent Zero as a pip-installable package with entry points.
**Why**: `pip install hyperagent0 && hyperagent0 start` is the simplest onboarding path. Entry points provide the CLI without manual PATH management.

### D5: sandbox_mode is per-project with a global default
**Choice**: `sandbox_mode` lives in two places. Global default in `settings.json`. Per-project override in `project.json` under a new `sandbox` block. Spec 01 defines the schema with the mode values it owns:
```json
{
  "sandbox": {
    "mode": "inherit | none | sandbox | ssh"
  }
}
```
Spec 05 extends the literal type with `docker | podman | cgroup` and adds the resource/network/image fields (`image`, `cpu`, `memory`, `network`, `persist_sandbox`, etc.). Schema is forward-compatible: the field is one nested object, spec 05 just adds keys and broadens the mode literal.

Resolution: `project.sandbox.mode == "inherit"` → use global; otherwise project wins. Default for new projects is `inherit`. Channel-routed and scheduler-spawned contexts both pick up the project's mode because they go through `projects.activate_project()` (`task_scheduler.py:784`, channel router per spec 04).

**Why**: A trusted internal project may want `mode=none` for speed; a scraping project may want `mode=sandbox` with a strict network allowlist (or, after spec 05 lands, `mode=docker` with `network=none`). Operators get a safety floor via the global default; project owners get per-workspace control.

### D6: Path translation lives inside sandbox_manager (deferred to spec 05)
**Choice**: `path_translate` is needed only by container-spawning backends (Docker/Podman) when the agent is itself in a container. Since those backends are deferred to spec 05, the helper moves there.
**Why**: `none` and `srt` (the modes this spec ships) both keep code in the agent's own filesystem namespace — no path translation needed. `srt` adds restrictions but doesn't remap paths. Premature to build the helper before there's a consumer.

### D7: Single wheel with optional extras
**Choice**: Ship one wheel — `hyperagent0` — with optional dependency groups (extras) for heavy or platform-specific deps. Core install is lean; users opt into channels, SDK adapters, and sandbox backends as needed.

Extras matrix (v1):

| Extra | Pulls in | Used by |
|-------|----------|---------|
| `[claude-sdk]` | `claude-agent-sdk` | spec 02 |
| `[telegram]` | `python-telegram-bot` | spec 04 |
| `[slack]` | `slack-bolt` | spec 04 |
| `[discord]` | `discord.py` | spec 04 |
| `[channels]` | telegram + slack + discord | bundle |
| `[all]` | every extra above | dev convenience |

`sandbox_mode=sandbox` requires its backend on PATH. The initial backend is `srt` (npm: `@anthropic-ai/sandbox-runtime`) — a Node CLI, not a Python dependency, so it's not part of the extras matrix. The setup wizard (spec 03 task 2.3) probes `shutil.which("srt")` and prints installation instructions if missing. `sandbox_mode=docker`/`podman` extras are added by spec 05 when those backends ship. `sandbox_mode=ssh` uses existing `paramiko` already in `requirements.txt`.

**Why**: One repo + one wheel keeps versioning trivial and avoids three-way dependency-resolver chaos between sub-packages. Extras isolate the heavy/optional deps so `pip install hyperagent0` stays small (~Flask + LiteLLM + stdlib). Multi-package split is a future option if a sub-component needs an independent release cadence — start single, split on demand.

**Entry points** declared in `pyproject.toml`:
- `hyperagent0 = "hyperagent0.cli:main"` (long form, for scripts/docs)
- `haz = "hyperagent0.cli:main"` (short alias, daily use)

Both resolve to the same Click group; spec 03 owns the subcommand surface and its lazy-loading behavior. Spec 01 only commits to the packaging contract.

### D8: `sandbox` mode uses `srt` as the initial backend; recommended default when available
**Choice**: The `sandbox` mode is implemented by [`anthropic-experimental/sandbox-runtime`](https://github.com/anthropic-experimental/sandbox-runtime) (`srt` CLI, npm: `@anthropic-ai/sandbox-runtime`, Apache-2.0). When `srt` is available on PATH, surface `sandbox` as the recommended `sandbox_mode` and have the setup wizard suggest it by default. Backend probe: `shutil.which("srt")`. Future backends (e.g., direct `bubblewrap`, `landlock`, `chrome-sandbox`) can register against the same `sandbox` mode without breaking config files.

**Why `srt` first**: It's the open-source OS-level sandbox Anthropic uses inside Claude Code itself for its bash tool. `sandbox-exec`+Seatbelt on macOS, `bubblewrap`+seccomp+netns on Linux — no container, no daemon, no image pulls. Per Anthropic's published numbers, it cut permission prompts by 84% in internal usage.

- **Lightweight**: near-zero startup, shares the agent's filesystem namespace.
- **macOS-friendly**: works without Docker Desktop.
- **Tight controls**: FS read (deny-then-allow) + FS write (allow-only) + network (allow-only via HTTP/SOCKS5 proxy) + Unix socket blocking by default.
- **No Python deps**: invoked as a subprocess. Our backend is a thin wrapper that generates a per-project settings JSON and runs `srt --settings ... <cmd>`.

**Limitations** (documented in setup docs):
- Native Windows not yet supported (WSL2 works as Linux).
- Linux requires `bubblewrap`, `socat`, `ripgrep` system packages.
- Glob patterns in FS rules work on macOS only; Linux uses literal paths.

**Why not collapse `sandbox` and the container modes?**: Container modes (spec 05) give stronger isolation (separate kernel view, ephemeral rootfs, image pinning for reproducibility). `sandbox` and container modes are different points on the isolation/cost curve, not substitutes.

### D9: Wrapper-package architecture (`hyperagent0/` wraps unchanged `python/`)
**Choice**: Keep the upstream-mirrored tree at `python/` **unrenamed** to preserve cherry-pick ergonomics. All net-new code (sandbox manager, daemon, channels, claude-sdk adapter, deployment_mode helpers) lives under a new top-level `hyperagent0/` package — the brand surface. The wheel name, the Python import name, and the long-form CLI binary all unify under `hyperagent0`; the short CLI alias remains `haz`.

```
hyperagent-zero/                    # repo dir (unchanged; the dashed dir name is fine, it's just where the code lives)
├── pyproject.toml                  # wheel: hyperagent0
├── hyperagent0/                    # ← OURS — net-new code, no upstream collision risk
│   ├── __init__.py                 # re-exports stable public API (Agent, AgentContext, ...)
│   ├── cli.py                      # Click group (spec 03)
│   ├── cli_commands/               # lazy subcommand modules (spec 03)
│   ├── daemon.py                   # daemon lifecycle (spec 03)
│   ├── runtime/
│   │   └── deployment_mode.py      # is_host_mode / is_docker_mode
│   ├── sandbox/                    # SandboxBackend ABC + registry + backends
│   │   ├── __init__.py
│   │   ├── none.py
│   │   ├── srt.py
│   │   ├── ssh.py
│   │   └── … (docker/podman/cgroup/path_translate added by spec 05)
│   ├── projects/
│   │   └── resolve.py              # resolve_sandbox_mode(global, project)
│   ├── channels/                   # spec 04 lives here
│   └── claude_sdk/                 # spec 02 lives here
├── python/                         # ← UPSTREAM-MIRRORED — minimal surgical patches only
│   ├── agent.py                    # unchanged
│   ├── models.py                   # unchanged
│   ├── helpers/
│   │   ├── settings.py             # PATCH: add sandbox_mode field; defaults
│   │   ├── runtime.py              # PATCH: delegate is_dockerized to hyperagent0.runtime
│   │   ├── projects.py             # PATCH: add sandbox block to ProjectData schema
│   │   └── … (rest unchanged)
│   └── tools/
│       └── code_execution_tool.py  # PATCH: route through hyperagent0.sandbox registry
├── run_ui.py                       # unchanged; invoked by hyperagent0.cli.start
└── …
```

**Conflict-surface budget for spec 01**: exactly **four** upstream files patched (`settings.py`, `runtime.py`, `projects.py`, `code_execution_tool.py`). All other new code is additive in `hyperagent0/`. Future specs follow the same rule: extend in `hyperagent0/`, patch upstream only when behavior must change at an existing call site.

**Rule of thumb**: if a new file would naturally live under `python/helpers/` or `python/tools/` because it's a helper for our features (channels, sandbox, daemon, etc.), put it under `hyperagent0/` instead. The `python/` tree is reserved for upstream code we haven't (yet) needed to modify.

**Why**: This is a major fork; we are not maintaining bidirectional compatibility with upstream `agent0ai/agent-zero`. But cherry-picking upstream bugfixes during the v2 stabilization period is still valuable, and the cost of preserving that ability is small (don't rename their files). The wrapper package gives us brand surface, a stable public API, and a clear "ours vs. theirs" line for every file. Once we accumulate enough patches in `python/` that cherry-picks become noisy regardless, we can revisit a fuller rename in a dedicated spec — but until then, the wrapper buys us optionality at near-zero cost.

## Tasks

### P1 — Must Do
- [ ] 1.1 Create `pyproject.toml` with proper entry points and extras
  - Package name `hyperagent0`; `requires-python = ">=3.11"`
  - Two console_scripts entry points both targeting `hyperagent0.cli:main`:
    - `hyperagent0` (long form)
    - `haz` (short alias)
  - Core deps: `dynamic = ["dependencies"]` reading existing `requirements.txt` (Flask, LiteLLM, etc.)
  - Optional extras per D7: `docker`, `podman`, `claude-sdk`, `telegram`, `slack`, `discord`, `channels`, `all`
  - Build backend: `setuptools>=68`; package layout per D9: `[tool.setuptools.packages.find]` discovers both the new `hyperagent0/` wrapper package and the unchanged `python/` upstream tree. Include both: `packages = ["hyperagent0", "hyperagent0.*", "python", "python.*"]` (or `find:` with both as roots)
  - Smoke test: `pip install -e . && haz --help && hyperagent0 --help` both work
  - Smoke test: `pip install -e .[all]` resolves without conflicts
- [ ] 1.2 Refactor `runtime.py` to support both modes cleanly
  - `is_host_mode()` / `is_docker_mode()` replacing `is_dockerized()`
  - Read `DEPLOYMENT_MODE` env var, auto-detect if inside Docker
  - [src:python/helpers/runtime.py]
- [ ] 1.3 Update `settings.py` defaults for host mode
  - `ssh_enabled=false`, `rfc_auto_docker=false`, `rfc_url=""` when host mode
  - New `sandbox_mode` field (default: `none`)
  - [src:python/helpers/settings.py]
- [ ] 1.4 Create `hyperagent0/sandbox/` package (ABC + spec-01 backends)
  - `hyperagent0/sandbox/__init__.py` — exports `SandboxBackend` ABC, `get_backend()`, `register_backend()` (lets spec 05 plug in additional backends)
  - `SandboxBackend` ABC: `open_shell(cwd) -> InteractiveSession`, `close()`, `is_available() -> bool` (classmethod)
  - `hyperagent0/sandbox/none.py` (`NoneBackend`) — returns existing `LocalInteractiveSession` unchanged (preserves today's behavior bit-for-bit). Implements `sandbox_mode=none`.
  - `hyperagent0/sandbox/srt.py` (`SandboxBackendSrt`) — wraps `LocalInteractiveSession` so the shell invocation is `srt --settings <profile.json> <command>` instead of running directly; probe via `shutil.which("srt")`. Implements `sandbox_mode=sandbox`. Per-project profile JSON generation: from `project.json#sandbox` plus defaults (workspace dir writable, system paths read-only, network policy from project config; defaults documented in setup docs).
  - `hyperagent0/sandbox/ssh.py` (`SshBackend`) — wraps existing `SSHInteractiveSession` (`python/helpers/ssh_shell.py`) for remote execution. Implements `sandbox_mode=ssh`. Reuses existing `code_exec_ssh_*` settings as connection params.
  - Backend registry: `get_backend(mode, project_dir) -> SandboxBackend`; raises with install hint if a backend's dependency is missing
  - Spec 05 will register `DockerBackend`, `PodmanBackend`, `CgroupBackend` against this same ABC via `register_backend()`
- [ ] 1.5 Modify code execution tool to use sandbox manager
  - Route through `sandbox_manager.get_backend(self.agent.config.sandbox_mode, project_dir=...)`
  - All four spec-01 modes (`none`, `sandbox`, `ssh`, plus the legacy fallback) go through the registry
  - Existing `code_exec_ssh_enabled` becomes a deprecation alias: `True` → `sandbox_mode=ssh` at config-load time, with a one-time deprecation warning logged
  - [src:python/tools/code_execution_tool.py]
- [ ] 1.6 Add per-project `sandbox` block to `project.json`
  - Extend `BasicProjectData` TypedDict with `ProjectSandboxSettings`. Spec 01 ships with one field: `mode: Literal["inherit", "none", "sandbox", "ssh"]`. Spec 05 broadens the literal and adds resource/network/image fields.
  - `resolve_sandbox_mode(global_settings, project_name)` helper: `inherit` → global, else project value
  - Plumb resolved mode into `AgentConfig` so `code_execution_tool` reads `self.agent.config.sandbox_mode`
  - Verify scheduler path: `task_scheduler.__new_context` → `projects.activate_project` → context picks up project mode
  - [src:python/helpers/projects.py, python/helpers/settings.py, initialize.py]

> Note: the original tasks 1.6 (sandbox Dockerfile) and 1.8 (path_translate helper) move to spec 05 along with the container-spawning backends. They have no consumer in spec 01.

### P2 — Should Do
- [ ] 2.1 Write host-mode installation docs
  - Update `docs/setup/installation.md` with pip install path
- [ ] 2.2 Test: host mode startup without Docker installed (no `docker` binary on PATH)
- [ ] 2.3 Test: `sandbox` mode end-to-end (FS write denial, network allowlist, restricted child can run a script)
- [ ] 2.4 Test: `ssh` mode reaches a remote shell using existing `code_exec_ssh_*` settings
- [ ] 2.5 Test: backward compat — Docker deployment unchanged when `DEPLOYMENT_MODE=docker`; `code_exec_ssh_enabled=true` legacy setting still works (auto-migrates to `sandbox_mode=ssh`)

### P3 — Nice to Have
- [ ] 3.1 Auto-detect optimal sandbox mode on first run
  - Order: `sandbox` (if `srt` on PATH) → `none`. (Spec 05 inserts `cgroup`/`docker`/`podman` ahead of `none` when those backends ship.)
  - Setup wizard suggests the detected mode with an install hint if a stronger one is one `npm install -g` away.

## Open Questions

- [x] Should `sandbox_mode` be global or per-project? **Resolved (D5): both.** Global default in `settings.json`, per-project override in `project.json` under a `sandbox` block, with `inherit` as the default project value. Spec 05 fills in resource-policy details on top of the same schema.
- [x] Can we flip defaults without breaking Docker users? Yes — auto-detect `DEPLOYMENT_MODE` from container env.
- [ ] Should `is_development()` (today: `not is_dockerized()`) be decoupled from container detection? Proposal: yes, gate it on explicit `--development` / `A0_DEV=1` so host-mode users don't get RFC dispatch enabled by accident. Behavior change worth a P2 task and a callout in the install docs.
- [x] Keep `SSHInteractiveSession` as `sandbox_mode=ssh`? **Yes** — `ssh` is now a first-class mode in D2 alongside `none` and `sandbox`. Represents the "remote process" point in the process-relationship taxonomy. Existing `code_exec_ssh_enabled=true` configs auto-migrate to `sandbox_mode=ssh` with a one-time deprecation warning (task 1.5).
- [x] Should the importable package be renamed from the current loose `python/` tree? **Resolved (D9): no rename — wrapper instead.** Net-new code lives under `hyperagent0/`; `python/` stays unrenamed for cherry-pick ergonomics. Upstream tree gets ~4 surgical patches in spec 01, all other additions are wrapper-scoped.

## Log

**2026-05-20** — Initial spec drafted from codebase review. Confirmed `ssh_enabled`, `rfc_auto_docker`, `LocalInteractiveSession` already exist in upstream code. Key files: `runtime.py`, `settings.py`, `code_execution_tool.py`.

**2026-05-20** — Revised after architectural review. Added three-axis diagram to Context (agent runtime / code execution / in-process listeners). Resolved per-project sandbox question (D5: global default + per-project override under `project.json#sandbox`). Added D6 for path translation as the load-bearing piece keeping axes 1 and 2 orthogonal. Split tasks: 1.7 adds project-level sandbox schema and resolution, 1.8 adds `path_translate` helper with mountinfo-based mapping. Surfaced two follow-up open questions: decoupling `is_development()` from container detection, and whether to retain `SSHInteractiveSession` as a fifth sandbox mode.

**2026-05-20** — Added D7 (single wheel + optional extras) and locked in the entry-point names. `pip install hyperagent0` ships a lean core; `[docker]`/`[telegram]`/etc. are opt-in. Two console_scripts (`hyperagent0` long form, `haz` short alias) both resolve to the same Click group owned by spec 03. Task 1.1 updated with the extras matrix and smoke tests. Filed an open question about renaming `python/` → `hyperagent0/` for layout consistency, deferred until after 1.1 lands.

**2026-05-20** — Added `srt` (Anthropic [`sandbox-runtime`](https://github.com/anthropic-experimental/sandbox-runtime)) as a sandbox mode and deferred `docker`/`podman`/`cgroup` to spec 05. New D8 makes `srt` the recommended non-none mode (lightweight, container-less, macOS-friendly, Anthropic-supported). D2 now explicitly defines mode semantics — `none`/`srt` keep code in-environment with the agent; spec 05's container modes spawn fresh isolated environments. This resolves the "agent in docker + sandbox docker = same container?" ambiguity: `sandbox_mode=docker` always means a freshly-spawned container, never agent-container reuse. Tasks pruned: 1.4 ships only the ABC + `NoneSandbox` + `SrtSandbox`; the Dockerfile (was 1.6) and `path_translate` (was 1.8) move to spec 05 with their consumers. Extras matrix trimmed (no `[docker]`/`[podman]` rows). `srt` is a Node CLI dep surfaced via probe + install hint, not a Python extra.

**2026-05-20** — Renamed mode `srt` → `sandbox` and promoted `ssh` to a first-class mode. Taxonomy is now by **process relationship to the agent**: `none` = local subprocess no wrapper, `sandbox` = local subprocess with OS-level restrictions (srt as initial backend), `ssh` = remote process. This makes the config-file knob (`sandbox_mode: "sandbox"`) self-explanatory and lets the implementation backend evolve without renaming. Existing `code_exec_ssh_enabled=true` configs auto-migrate to `sandbox_mode=ssh` with a deprecation warning. Resolves the prior open question about retaining `SSHInteractiveSession`.

**2026-05-20** — Locked in unified `hyperagent0` branding (wheel name, Python import name, long-form CLI binary all `hyperagent0`; short CLI alias remains `haz`). Added D9 establishing the wrapper-package architecture: net-new code lives under a new `hyperagent0/` top-level package; the upstream-mirrored `python/` tree stays unrenamed for cherry-pick ergonomics; spec 01 budget is ~4 surgical patches in `python/` (`settings.py`, `runtime.py`, `projects.py`, `code_execution_tool.py`). The prior open question about renaming `python/` is resolved by wrapping rather than renaming. This is a major fork — bidirectional compatibility with upstream `agent0ai/agent-zero` is not a goal — but preserving cherry-pick ability during v2 stabilization is cheap and worth it.
