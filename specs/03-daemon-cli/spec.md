---
spec_id: 03-daemon-cli
status: DRAFT
since: 2026-05-20
until: null
epic: devops
features: [cli-entrypoint, daemon-mode, systemd-integration, setup-wizard]
supersedes: []
superseded_by: null
depends_on: [01-host-first]
---

# Daemon & CLI

## Context

Agent Zero has no proper process management. Starting it means running `python run_ui.py` in a terminal. There is no single command to start/stop/restart, no daemon mode, no idempotent operations, no status checking, no systemd integration. For a tool you run persistently, this is a must-have.

## Constraints

- `click` for CLI (lightweight, well-known)
- PID/lock management via `flock` (no race conditions)
- Must work without root: systemd `--user` units, `~/.hyperagent-zero/` for state
- `run_ui.py` must remain importable as a module (not just a standalone script)
- Idempotent: `start` when running = no-op, `stop` when stopped = no-op

## Decisions

### D1: Click-based CLI
**Choice**: `click` command group with subcommands.
**Why**: Standard Python CLI library. Supports subcommands, help generation, argument validation. Already widely known.

### D2: flock-based PID management
**Choice**: `fcntl.flock()` on `~/.hyperagent-zero/hyperagent-zero.lock` for singleton enforcement.
**Why**: Atomic, no race conditions between PID file write and process start. Works across all Unix systems.

### D3: systemd user units
**Choice**: `hyperagent-zero install-service` generates `~/.config/systemd/user/hyperagent-zero.service`.
**Why**: Auto-start on login, proper log management via journald, restart-on-failure. User-level (no root).

## Tasks

### P1 — Must Do
- [ ] 1.1 Create `cli.py` with Click command group
  - Subcommands: `start`, `stop`, `restart`, `status`, `logs`, `setup`, `config`
  - Entry point registered in pyproject.toml
- [ ] 1.2 Create `python/helpers/daemon.py` with PID/flock management
  - `acquire_lock()`, `release_lock()`, `is_running()`, `get_pid()`
  - PID file at `~/.hyperagent-zero/hyperagent-zero.pid`
  - Lock file at `~/.hyperagent-zero/hyperagent-zero.lock`
- [ ] 1.3 Refactor `run_ui.py` to be importable as a module
  - Extract `start_server(host, port, **kwargs)` function
  - Keep `if __name__ == "__main__"` for backward compat
  - [src:run_ui.py]
- [ ] 1.4 Implement `start` command
  - Foreground (default) and daemon mode (`-d`/`--daemon`)
  - Daemon: double-fork, redirect stdout/stderr to `~/.hyperagent-zero/logs/`
  - Idempotent: if already running, print status and exit 0
- [ ] 1.5 Implement `stop` command
  - Read PID from file, send SIGTERM
  - Wait up to 30s for graceful shutdown
  - Idempotent: if not running, print "not running" and exit 0
- [ ] 1.6 Implement graceful shutdown handler
  - On SIGTERM/SIGINT: pause all AgentContexts, wait for in-flight tools, persist state, close connections
  - [src:agent.py] — register shutdown hook

### P2 — Should Do
- [ ] 2.1 Implement `status` command
  - Show: running/stopped, PID, uptime, port, active contexts count
  - `--json` flag for machine-readable output
- [ ] 2.2 Implement `logs` command
  - `--follow/-f` for tail, `--since DURATION`, `--lines N`
  - Read from `~/.hyperagent-zero/logs/`
- [ ] 2.3 Implement `setup` wizard
  - Interactive: API key, model selection, port, sandbox_mode
  - Write to settings file
- [ ] 2.4 Create systemd unit generator
  - `hyperagent-zero install-service` / `uninstall-service`
  - Template: `~/.config/systemd/user/hyperagent-zero.service`
- [ ] 2.5 Test: start/stop/restart idempotency
- [ ] 2.6 Test: daemon mode with log redirection
- [ ] 2.7 Test: graceful shutdown preserves agent state

### P3 — Nice to Have
- [ ] 3.1 `hyperagent-zero config show/set/edit` subcommands
- [ ] 3.2 `hyperagent-zero project list/create/delete/switch` subcommands
- [ ] 3.3 Auto-restart on crash (daemon watchdog)

## Open Questions

- [ ] Should logs rotate automatically? Probably yes — use `logging.handlers.RotatingFileHandler`.
- [ ] Should the CLI support multiple simultaneous instances (different ports)? Probably not for v1.

## Log

**2026-05-20** — Initial spec. `run_ui.py` is currently a monolithic script that initializes Flask, Socket.IO, and starts uvicorn. Needs refactoring to extract `start_server()`.
