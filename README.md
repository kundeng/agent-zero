# HyperAgent Zero

> Personal agentic framework. Forked from [agent0ai/agent-zero](https://github.com/agent0ai/agent-zero)
> and re-shaped as a **host-first hyperagent harness**: the agent runs on
> your host (or VM) and uses lightweight containers only when it needs to
> sandbox code execution.

What this fork adds on top of upstream agent-zero:

| | |
|---|---|
| Host-first architecture | Agent runs natively; containers are a per-project sandbox option, not a requirement |
| Claude Agent SDK provider | First-class alongside LiteLLM, with thinking-block support |
| Daemon + CLI (`haz` / `hyperagent0`) | `start / stop / status / logs / setup / config` — lazy-loaded, &lt;200ms cold start |
| Chat channel adapters | Telegram, Slack, Discord with mention-aware routing |
| Project isolation | Per-project sandbox backends: `none` / `sandbox` / `ssh` / `cgroup` / `docker` / `podman` |

---

## Install

Three supported paths. Pick the one that matches how you want to run the
agent. **In all three, the LLM provider is configured later, in the web
UI** — exactly like upstream agent-zero.

### Option 1: Docker, no host install

The fastest way to try it. Single command, nothing touches your host
outside of Docker.

```bash
docker run --rm -it -p 50080:50080 -v hyperagent0:/app/memory \
    bayeslearner/hyperagent0:latest
```

Then open <http://localhost:50080> and configure your LLM in **Settings**.
The named volume `hyperagent0` keeps chat history and memory across
restarts.

### Option 2: Install on this machine (host or VM)

Single command — the script handles everything, including fetching the
source. No need to clone a repository yourself.

```bash
curl -fsSL https://raw.githubusercontent.com/kundeng/hyperagent-zero/v2-hyperagent/install.sh | bash
```

Then:

```bash
haz start              # foreground; -d to daemonize
```

Open <http://localhost:50080>, pick your LLM provider in **Settings**.

Where things land:

| Path | What's there |
|------|--------------|
| `~/.hyperagent0/repo/` | The checked-out source tree (so runtime assets like prompts/ are reachable) |
| `~/.hyperagent0/venv/` | Python venv with the agent installed |
| `~/.local/bin/haz`     | Symlink into the venv's entry point |

Requirements: Python 3.12+, git. The script will tell you if either is
missing and how to install it for your OS.

**To pick per-project container isolation**, just toggle the sandbox
mode in the UI's Settings (or run `haz config set sandbox_mode docker`)
after install — same install path either way.

### Option 3: Compose, no host clone

For a persistent, restart-on-boot deployment. Download two files, set
your secrets, run compose. No repository checkout needed.

```bash
mkdir hyperagent0 && cd hyperagent0
curl -fsSLO https://raw.githubusercontent.com/kundeng/hyperagent-zero/v2-hyperagent/docker/hyperagent0/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/kundeng/hyperagent-zero/v2-hyperagent/docker/hyperagent0/.env.example -o .env
$EDITOR .env                        # optional: set API keys, port
docker compose up -d
```

Then open <http://localhost:50080>. The compose stack pulls
`bayeslearner/hyperagent0:latest`, mounts three named volumes for state,
and restarts on host reboot.

---

## Updating

| Installed via | Update with |
|---------------|-------------|
| Option 1 (`docker run`) | `docker pull bayeslearner/hyperagent0:latest`, then re-run |
| Option 2 (`curl ... \| bash`) | Re-run the same one-liner, or just `git -C ~/.hyperagent0/repo pull` |
| Option 3 (compose) | `docker compose pull && docker compose up -d` |

**About the curl one-liner upgrade**: install.sh installs hyperagent0
editable against `~/.hyperagent0/repo`, so a plain `git pull` of that
checkout is the cheapest update — your `haz` binary points into the
repo directly, so source changes show up immediately. Re-running the
one-liner does the same `git pull` PLUS refreshes any dependency
changes; safe to run any time.

---

## CLI surface (Option 2)

```text
haz start           # launch the daemon (foreground; -d to background, --systemd for unit)
                    #   --lan        bind on 0.0.0.0 so the UI is reachable from the LAN
                    #   --host HOST  pick a specific bind address
                    #   --port PORT  override the default of 50080
haz stop            # graceful shutdown
haz restart         # stop + start
haz status          # PID / uptime / port - does NOT import LiteLLM, stays fast
haz logs            # tail ~/.hyperagent0/logs/daemon.log
haz check           # ping the configured LLM, report OK + latency (or a diagnosis)
haz setup           # OPTIONAL: interactive settings wizard (you can just use the UI)
haz config          # get / set / path - read or write usr/settings.json fields
```

Both `haz` and `hyperagent0` are registered and behave identically. The
group is lazy-loaded so `--help` and `status` stay under 200ms.

**LAN access**: by default the daemon binds to `127.0.0.1`. To reach
the UI from another machine on your network:

```bash
haz stop                   # if it's running
haz start -d --lan         # binds to 0.0.0.0
# Then visit http://<this-host's-IP>:50080 from anywhere on the LAN.
```

**Sanity check the LLM** after you configure it in the UI:

```bash
haz check                  # OK (0.42s) — openai/cc/claude-sonnet-4-6 responded.
```

---

## Developer setup

If you want to contribute or run from a local checkout:

```bash
git clone https://github.com/kundeng/hyperagent-zero
cd hyperagent-zero
./install.sh --dev          # editable install pointing at this checkout
source ~/.hyperagent0/venv/bin/activate
python -m pytest tests/ -q
```

Branch `v2-hyperagent` holds all our changes; `main` tracks upstream
agent-zero. See [CLAUDE.md](CLAUDE.md) for conventions, and
[specs/](specs/) for the design documents driving the fork.

---

## Repository layout

```text
hyperagent-zero/
├── agent.py                  # upstream agent core (untouched, cherry-pickable)
├── models.py                 # upstream LLM layer
├── run_ui.py                 # upstream Flask + Socket.IO entry point
├── python/                   # upstream tools, extensions, helpers
├── prompts/, agents/, ...    # upstream runtime assets
│
├── hyperagent0/              # all net-new code lives here
│   ├── cli.py                # haz / hyperagent0 entry point
│   ├── cli_commands/         # lazy-loaded subcommands
│   ├── daemon.py             # PID file, lock, signal handling
│   ├── sandbox/              # spec 05 sandbox backends
│   ├── channels/             # spec 04/06 chat adapters
│   ├── claude_sdk/           # spec 02 Claude Agent SDK provider
│   └── projects/             # spec 05 per-project state
│
├── docker/
│   ├── hyperagent0/          # full daemon image (Options 1 and 3)
│   └── sandbox/              # slim exec-only image
│
├── install.sh                # one-command host installer (Option 2)
├── specs/                    # design documents 01–06
└── tests/                    # pytest suite
```

---

## License

Apache-2.0. See [LICENSE](LICENSE). Upstream agent-zero is also Apache-2.0.
The original upstream README is preserved at [README.upstream.md](README.upstream.md).
