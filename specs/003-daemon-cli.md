---
title: "Daemon & CLI"
status: draft
priority: P1
breaks_compat: false
depends_on: ["001-host-first-architecture"]
---

# Spec 003: Daemon & CLI

## Problem

Agent Zero has no proper process management. Starting it means running `python run_ui.py` in a terminal. There is no:

- Single command to start/stop/restart
- Daemon mode (background process)
- Idempotent operations (running start twice doesn't break things)
- Status checking
- Log tailing
- Graceful shutdown with state persistence
- systemd integration for auto-start on boot

## Requirements

### R1: Single CLI entrypoint
```bash
hyperagent-zero start          # start in foreground (default)
hyperagent-zero start -d       # start as daemon (background)
hyperagent-zero stop            # graceful shutdown
hyperagent-zero restart         # stop + start
hyperagent-zero status          # show running state, uptime, active contexts
hyperagent-zero logs            # tail logs (like docker logs -f)
hyperagent-zero logs --since 1h # recent logs
```

### R2: Idempotent operations
- `start` when already running: prints status, exits 0 (not error)
- `stop` when not running: prints "not running", exits 0
- `start -d` when already running as daemon: prints "already running (PID xxx)", exits 0
- No PID file races: use flock-based locking

### R3: Daemon mode
- Daemonize via `python-daemon` or double-fork
- PID file at `~/.hyperagent-zero/hyperagent-zero.pid`
- Stdout/stderr redirected to log file at `~/.hyperagent-zero/logs/`
- Graceful shutdown on SIGTERM (persist agent state, close connections)
- Auto-restart on crash (optional, via `--auto-restart` flag)

### R4: systemd integration (Linux)
- `hyperagent-zero install-service` generates and installs a systemd user unit
- `hyperagent-zero uninstall-service` removes it
- Unit file: `~/.config/systemd/user/hyperagent-zero.service`
- `systemctl --user enable hyperagent-zero` for auto-start on login

### R5: Friendly installation
```bash
# Option A: pip (recommended)
pip install hyperagent-zero
hyperagent-zero setup           # interactive first-time setup (API keys, model selection)

# Option B: git clone (development)
git clone https://github.com/kundeng/hyperagent-zero
cd hyperagent-zero
pip install -e .
hyperagent-zero setup
```

## Design

### CLI structure

```
hyperagent-zero (click-based CLI)
├── start [--daemon/-d] [--port PORT] [--host HOST]
├── stop [--force]
├── restart [--daemon/-d]
├── status [--json]
├── logs [--follow/-f] [--since DURATION] [--lines N]
├── setup                    # interactive first-time config
├── install-service          # systemd unit installation
├── uninstall-service
├── project                  # project management subcommand group
│   ├── list
│   ├── create NAME
│   ├── delete NAME
│   └── switch NAME
└── config                   # settings management
    ├── show
    ├── set KEY VALUE
    └── edit                 # open settings in $EDITOR
```

### Key files

| File | Purpose |
|------|---------|
| NEW: `cli.py` | Click-based CLI entry point |
| NEW: `daemon.py` | Daemon lifecycle (start/stop/PID/flock) |
| NEW: `systemd_unit.py` | Generate systemd unit file |
| `pyproject.toml` | `[project.scripts] hyperagent-zero = "cli:main"` |
| `run_ui.py` | Refactored to be importable (not just a script) |

### PID file and locking

```python
# daemon.py sketch
import fcntl

LOCK_FILE = Path("~/.hyperagent-zero/hyperagent-zero.lock").expanduser()
PID_FILE  = Path("~/.hyperagent-zero/hyperagent-zero.pid").expanduser()

def acquire_lock() -> bool:
    """Non-blocking flock. Returns True if acquired."""
    fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        return True
    except BlockingIOError:
        return False
```

### Graceful shutdown

On SIGTERM/SIGINT:
1. Set `AgentContext.paused = True` for all active contexts
2. Wait up to 30s for in-flight tool executions to complete
3. Persist agent state (conversation history, memory)
4. Close WebSocket connections with close frame
5. Stop accepting new connections
6. Exit 0

### systemd unit template

```ini
[Unit]
Description=HyperAgent Zero
After=network.target

[Service]
Type=simple
ExecStart={python_path} -m hyperagent_zero start --port {port}
ExecStop={python_path} -m hyperagent_zero stop
Restart=on-failure
RestartSec=5
WorkingDirectory={install_dir}
Environment=DEPLOYMENT_MODE=host

[Install]
WantedBy=default.target
```

## Tasks

- [ ] Create `cli.py` with Click command group
- [ ] Create `daemon.py` with PID/flock management
- [ ] Refactor `run_ui.py` to be importable as a module
- [ ] Implement graceful shutdown handler (SIGTERM/SIGINT)
- [ ] Create `systemd_unit.py` template generator
- [ ] Add `setup` wizard (interactive API key + model config)
- [ ] Add `pyproject.toml` entry points
- [ ] Test: start/stop/restart idempotency
- [ ] Test: daemon mode with log redirection
- [ ] Test: systemd unit install/uninstall
- [ ] Test: graceful shutdown preserves agent state
