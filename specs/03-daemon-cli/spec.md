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

This spec owns the entire CLI surface (`haz` and `hyperagent0`) and daemon lifecycle. Spec 01 (D7) owns the packaging contract and registers the two entry points; both resolve to the Click group defined here. Channels (spec 04), scheduler, and web UI all run *inside* the daemon process as in-process async tasks — the daemon is what keeps them alive between sessions.

## Constraints

- `click` for CLI (lightweight, well-known)
- PID/lock management via `flock` (no race conditions)
- Must work without root: systemd `--user` units, `~/.hyperagent0/` for state
- `run_ui.py` must remain importable as a module (not just a standalone script)
- Idempotent: `start` when running = no-op, `stop` when stopped = no-op
- **CLI cold-start must stay snappy.** `haz status` should not import `discord.py`, `python-telegram-bot`, `docker`, or LiteLLM. Target: `haz --help` and `haz status` both return in < 200ms on a warm filesystem.
- Two entry points (`haz`, `hyperagent0`) resolve to the same Click group; behavior is identical regardless of which name was used.

## Decisions

### D1: Click-based CLI
**Choice**: `click` command group with subcommands.
**Why**: Standard Python CLI library. Supports subcommands, help generation, argument validation. Already widely known.

### D2: flock-based PID management
**Choice**: `fcntl.flock()` on `~/.hyperagent0/hyperagent0.lock` for singleton enforcement.
**Why**: Atomic, no race conditions between PID file write and process start. Works across all Unix systems.

### D3: systemd user units
**Choice**: `hyperagent0 install-service` generates `~/.config/systemd/user/hyperagent0.service`.
**Why**: Auto-start on login, proper log management via journald, restart-on-failure. User-level (no root).

### D4: `haz` no-args prints status, never silently starts
**Choice**: `haz` with no subcommand runs the equivalent of `haz status` and prints a short hint line (`Run 'haz start' to launch the daemon, or 'haz --help' for all commands.`). It does **not** auto-start the daemon, and it does **not** open the web UI.
**Why**: Silently starting a long-lived process from a bare-command invocation is the kind of side effect that surprises users and breaks scripts. Status is cheap, safe, and discoverable. Users who want one-shot launch can type the four extra characters.

### D5: Lazy subcommand loading
**Choice**: Click subcommands declared via lazy loading (e.g. `click.Group` with `MultiCommand.list_commands` + per-command `import_module` on invocation, or `click-plugins` style). Top-level `cli.py` imports only `click` and stdlib. Heavy imports (`docker`, channel SDKs, LiteLLM, Flask) happen *inside* the subcommand that needs them.
**Why**: Honors the cold-start constraint. `haz status` needs to read a PID file and maybe poll a Unix socket — it has no business importing `discord.py`. Concretely: `haz start` is allowed to be slow (it's launching a server); `haz status`, `haz stop`, `haz logs`, `haz --help` must not be.

### D6: Default to foreground; daemonize with `-d`
**Choice**: `haz start` runs foreground by default (logs to stdout, Ctrl-C stops cleanly). `haz start -d` / `--daemon` double-forks and detaches. `haz start --systemd` is a hint for systemd unit invocation (foreground but with journald-friendly logging — no double-fork, no daemonization).
**Why**: Foreground is the most useful behavior for first-time users and for `tmux`/`screen` users. Daemonization is opt-in. Systemd handles its own lifecycle and expects foreground processes — a separate flag keeps the signaling unambiguous.

## CLI surface (target shape)

```
haz                              # = haz status + hint (D4); never starts daemon
haz --help                       # discoverable subcommand tree
haz --version
haz start [-d|--daemon] [--systemd] [--port N] [--host H]
haz stop [--timeout 30]
haz restart
haz status [--json]
haz logs [-f|--follow] [--since DUR] [--lines N]
haz setup                        # interactive wizard
haz exec "<prompt>" [--project P]  # one-shot, no daemon (P2)
haz config get|set|edit          # P2
haz project list|create|delete|switch [name]   # P3
haz channel list|enable|disable [name]         # P3 (spec 04 adds bodies)
haz install-service              # systemd unit
haz uninstall-service
```

`hyperagent0` is the same group under its long-form name — every command above is reachable identically via either binary.

## Tasks

### P1 — Must Do
- [ ] 1.1 Create `hyperagent0/cli.py` with a lazy-loading Click group
  - Root group imports only `click` + stdlib (cold-start budget per D5)
  - Subcommands registered lazily: `start`, `stop`, `restart`, `status`, `logs`, `setup`, `config`
  - `haz` with no subcommand → invoke `status` + print hint line (D4)
  - Each subcommand lives in its own module under `hyperagent0/cli_commands/`
  - Smoke test: `time haz --help` and `time haz status` both < 200ms
  - Both entry points (`haz`, `hyperagent0`) wired in pyproject.toml (spec 01 task 1.1) target this module's `main`
- [ ] 1.2 Create `python/helpers/daemon.py` with PID/flock management
  - `acquire_lock()`, `release_lock()`, `is_running()`, `get_pid()`
  - PID file at `~/.hyperagent0/hyperagent0.pid`
  - Lock file at `~/.hyperagent0/hyperagent0.lock`
- [ ] 1.3 Refactor `run_ui.py` to be importable as a module
  - Extract `start_server(host, port, **kwargs)` function
  - Keep `if __name__ == "__main__"` for backward compat
  - [src:run_ui.py]
- [ ] 1.4 Implement `start` command
  - Foreground (default) — logs to stdout, Ctrl-C clean shutdown
  - `-d`/`--daemon` — double-fork, redirect stdout/stderr to `~/.hyperagent0/logs/`
  - `--systemd` — foreground but journald-friendly (structured logs, no daemonization, no PID file write — systemd owns the PID)
  - Idempotent: if already running (via flock check), print status and exit 0
  - Heavy imports (`start_server`, LiteLLM, Flask) happen inside this command's module, not at CLI startup
- [ ] 1.5 Implement `stop` command
  - Read PID from file, send SIGTERM
  - Wait up to 30s for graceful shutdown
  - Idempotent: if not running, print "not running" and exit 0
- [ ] 1.6 Implement graceful shutdown handler
  - On SIGTERM/SIGINT: pause all AgentContexts, wait for in-flight tools, persist state, close connections
  - [src:agent.py] — register shutdown hook

### P2 — Should Do
- [ ] 2.1 Implement `status` command
  - Show: running/stopped, PID, uptime, port, active contexts count, sandbox_mode, deployment_mode
  - `--json` flag for machine-readable output
  - Talks to a running daemon over a Unix socket at `~/.hyperagent0/daemon.sock` for live counts; falls back to PID-only when daemon is down
  - Must not import LiteLLM, channels, or sandbox SDKs (cold-start budget)
- [ ] 2.2 Implement `logs` command
  - `--follow/-f` for tail, `--since DURATION`, `--lines N`
  - Read from `~/.hyperagent0/logs/`
- [ ] 2.3 Implement `setup` wizard
  - Interactive: API key, model selection, port, sandbox_mode, deployment_mode hint
  - Write to settings file
- [ ] 2.4 Create systemd unit generator
  - `haz install-service` / `haz uninstall-service`
  - Template: `~/.config/systemd/user/hyperagent0.service`, invokes `haz start --systemd`
- [ ] 2.5 Implement `haz exec "<prompt>"` one-shot mode
  - Runs an agent monologue without starting the daemon or web UI
  - `--project P` activates project; default uses no-project context
  - Streams output to stdout; exits when monologue completes
  - This is the path that makes `haz` useful in scripts/pipelines
- [ ] 2.6 Test: start/stop/restart idempotency
- [ ] 2.7 Test: daemon mode with log redirection
- [ ] 2.8 Test: graceful shutdown preserves agent state
- [ ] 2.9 Test: cold-start budget — `time haz --help`, `time haz status`, `time haz stop` all < 200ms when no heavy SDKs are installed

### P3 — Nice to Have
- [ ] 3.1 `haz config get/set/edit` subcommands
- [ ] 3.2 `haz project list/create/delete/switch` subcommands
- [ ] 3.3 `haz channel list/enable/disable` subcommands (bodies live in spec 04)
- [ ] 3.4 Shell completion for `haz` and `hyperagent0` via Click's built-in completion (bash, zsh, fish)
- [ ] 3.5 Auto-restart on crash (daemon watchdog)

## Open Questions

- [ ] Should logs rotate automatically? Probably yes — use `logging.handlers.RotatingFileHandler`.
- [ ] Should the CLI support multiple simultaneous instances (different ports)? Probably not for v1.
- [ ] Should `haz exec` (one-shot mode) reuse a running daemon if one is up, or always spawn fresh? Reuse is faster (no model warm-up) but couples script invocations to daemon state. Fresh is predictable but slow. Lean toward fresh for v1.
- [ ] Cold-start budget enforcement: assert in CI via `python -X importtime` snapshot, or just spot-check? Snapshot catches regressions but adds CI flakiness on slow runners.

## Log

**2026-05-20** — Initial spec. `run_ui.py` is currently a monolithic script that initializes Flask, Socket.IO, and starts uvicorn. Needs refactoring to extract `start_server()`.

**2026-05-20** — Renamed package, wheel, and long-form CLI binary from `hyperagent-zero` / `hyperagent_zero` to unified `hyperagent0` per spec 01 D9. Short CLI alias `haz` unchanged. State dir is now `~/.hyperagent0/`. Systemd unit is `hyperagent0.service`. All `cli_commands/` modules live under the `hyperagent0/` wrapper package per the new fork architecture; `python/` upstream tree stays untouched by spec 03 entirely (spec 03's surface is purely additive).

**2026-05-20** — Absorbed CLI ergonomics from spec 01 review. Added D4 (`haz` no-args → status + hint, never silently starts), D5 (lazy subcommand loading, < 200ms cold-start budget for non-launch commands), D6 (foreground default, `-d` for daemon, `--systemd` for unit invocation). Documented the full target CLI surface up front. Task 1.1 restructured around lazy command modules under `hyperagent0/cli_commands/`. New task 2.5 (`haz exec` one-shot) makes the CLI useful for scripts/pipelines without a daemon. New task 2.9 enforces the cold-start budget. Open questions added for `haz exec` daemon reuse and CI enforcement of the budget.
