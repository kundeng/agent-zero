# HyperAgent Zero

Fork of [agent0ai/agent-zero](https://github.com/agent0ai/agent-zero) — a personal agentic framework — re-shaped as a host-first hyperagent harness with project isolation, Claude Agent SDK support, daemon lifecycle, chat channel integration, and a one-command install UX.

specs_root: specs/

## Branch Strategy

- `main` — tracks upstream `agent0ai/agent-zero`. Never commit directly.
- `v2-hyperagent` — our development branch. All specs implemented here.
- Cherry-pick upstream fixes: `git cherry-pick <sha>` from main into v2-hyperagent.

## Install (canonical paths)

End users — see [README.md](README.md). One-line summary of the three flows:

| Flow | Command | When |
|------|---------|------|
| Docker run | `docker run --rm -it -p 50080:50080 bayeslearner/hyperagent0:latest` | Trial / no host install |
| Host install | `curl -fsSL https://raw.githubusercontent.com/kundeng/hyperagent-zero/v2-hyperagent/install.sh \| bash` | Persistent install on a host or VM |
| Compose | curl two files (`docker-compose.yml` + `.env.example`), `docker compose up -d` | Production-ish |

Developer setup: `git clone … && ./install.sh --dev`. Installs editable against the local checkout under `~/.hyperagent0/venv` (or `--prefix DIR`).

### Install layout (Journey B)

```
~/.hyperagent0/
├── repo/        cloned source (provides agent.py, prompts/, etc.)
├── venv/        Python venv; hyperagent0 installed editable
└── logs/        daemon.log
~/.local/bin/{haz,hyperagent0}   symlinks into the venv
```

Critical: `pip install hyperagent0` from the wheel alone does NOT work — the wheel deliberately excludes upstream `python/` and asset directories (spec 07 D4). `install.sh` adds the repo to `sys.path` via a `.pth` file. See `hyperagent0/paths.py` for the resolver.

## Upstream Architecture (inherited)

### Core Agent Loop
`agent.py` → `Agent.monologue()` → tight loop: prepare_prompt → call_chat_model (LiteLLM) → process_tools → loop until break_loop=True.

### Extension System
`python/helpers/extension.py` — `@extensible` decorator fires `start`/`end` extension points around any method. Extensions are Python classes in numbered files (`_10_`, `_50_`, `_90_` = priority). Key extension folders under `python/extensions/`:
- `agent_init/`, `system_prompt/`, `monologue_start/`, `monologue_end/`
- `tool_execute_before/`, `tool_execute_after/`
- `before_main_llm_call/`, `message_loop_start/`, `message_loop_end/`
- `process_chain_end/`, `user_message_ui/`

### LLM Layer
`models.py` → `LiteLLMChatWrapper` → LiteLLM `acompletion()`. Provider/model/api_base configurable via settings. Rate limiting in `python/helpers/rate_limiter.py`.

### Project Model
`python/helpers/projects.py` — projects live in `usr/projects/<name>/` with `.a0proj/` containing:
- `project.json` (title, description, instructions, color, git_url, file_structure)
- `instructions/` (additional .md files injected into system prompt)
- `knowledge/` (RAG knowledge base)
- `variables.env`, `secrets/`
- `agents.json` (per-project sub-agent overrides)
- `agents/<profile>/extensions/python/` (per-project extensions)

### Tools
`python/tools/` — Python classes inheriting `Tool`. Key tools: `code_execution_tool.py`, `call_subordinate.py`, `skills_tool.py`, `memory_save.py`, `memory_load.py`, `scheduler.py`, `browser_agent.py`.

### Code Execution
`python/tools/code_execution_tool.py` routes through the sandbox backend registry. Active mode is set by `sandbox_mode` (spec 01 D2). Backends:

| `sandbox_mode` | Process relationship | Backend |
|----------------|----------------------|---------|
| `none` | Local subprocess, no wrapper | `hyperagent0/sandbox/none.py` |
| `sandbox` | Local subprocess, OS restrictions | `hyperagent0/sandbox/srt.py` (Anthropic `srt`) |
| `ssh` | Remote process | `hyperagent0/sandbox/ssh.py` |
| `cgroup` | Local, cgroup-restricted | `hyperagent0/sandbox/cgroup.py` (spec 05) |
| `docker` | Sibling container | `hyperagent0/sandbox/docker.py` (spec 05) |
| `podman` | Sibling container (rootless) | `hyperagent0/sandbox/podman.py` (spec 05) |

Legacy `ssh_enabled=true` auto-migrates to `sandbox_mode=ssh` with a one-time deprecation warning.

### MCP Support
`python/helpers/mcp_handler.py` — full MCP client: stdio, SSE, streamable-http transports. MCP tools surface as agent tools.

### Skills
`python/helpers/skills.py` — Anthropic open-standard SKILL.md with frontmatter. Loaded from `skills/`, `usr/skills/`, per-project, per-agent paths. `python/tools/skills_tool.py` exposes list/load methods.

### Sub-agents
`python/tools/call_subordinate.py` — spawns new `Agent` with `number+1`. **Blocking**: `await subordinate.monologue()`. Profiles in `agents/<profile>/`.

### Settings
`python/helpers/settings.py` — all config. Key fields: `chat_model_provider`, `chat_model_name`, `chat_model_api_base`, `sandbox_mode`, `ssh_enabled` (deprecated), `rfc_auto_docker`, `rfc_url`.

### Web UI
`run_ui.py` — Flask + Socket.IO via uvicorn. Static files from `webui/`. Real-time streaming, context management, file browser.

## Key Source Files

| File | Purpose |
|------|---------|
| `agent.py` | Core: AgentContext, Agent, monologue loop (~1040 lines) |
| `models.py` | LLM abstraction, LiteLLMChatWrapper (~844 lines) |
| `initialize.py` | Bootstrap, AgentConfig construction |
| `run_ui.py` | Web UI entry point, Flask + Socket.IO |
| `python/helpers/extension.py` | @extensible decorator, extension system |
| `python/helpers/settings.py` | All configuration |
| `python/helpers/runtime.py` | is_dockerized(), RFC dispatch |
| `python/helpers/projects.py` | Project CRUD, activation, prompt injection |
| `python/helpers/mcp_handler.py` | MCP client (stdio/SSE/streamable-http) |
| `python/helpers/skills.py` | SKILL.md parser, skill discovery |
| `python/tools/code_execution_tool.py` | Code exec: routes through `hyperagent0.sandbox` backends |
| `python/tools/call_subordinate.py` | Sub-agent spawning (blocking) |
| `python/tools/skills_tool.py` | Skill list/load tool |
| `hyperagent0/cli.py` | `haz` / `hyperagent0` Click group (lazy-loaded) |
| `hyperagent0/paths.py` | Resolves repo root for non-editable installs (spec 07) |
| `hyperagent0/daemon.py` | PID file, lock, signal handling |
| `hyperagent0/sandbox/` | Sandbox backend registry + per-mode implementations |
| `hyperagent0/channels/` | Telegram/Slack/Discord adapters + router |
| `install.sh` | One-command host installer (curl|bash entry point) |

## What We're Building (v2-hyperagent specs)

| Spec | Status | What it does | Depends on |
|------|--------|-------------|------------|
| [01-host-first](specs/01-host-first/spec.md) | DRAFT | Agent runs on host, sandbox only for code exec | — |
| [02-claude-sdk](specs/02-claude-sdk/spec.md) | DRAFT | Claude Agent SDK as first-class provider alongside LiteLLM | — |
| [03-daemon-cli](specs/03-daemon-cli/spec.md) | DRAFT | `hyperagent0 start/stop/status` (alias `haz`), systemd, pip install | 01 |
| [04-chat-channels](specs/04-chat-channels/spec.md) | DRAFT | Telegram, Slack, Discord channel adapters | 01, 03 |
| [05-project-isolation](specs/05-project-isolation/spec.md) | DRAFT | Per-project cgroup/Docker sandboxing with resource limits | 01 |
| [06-channel-hardening](specs/06-channel-hardening/spec.md) | DRAFT | Mention-aware routing, reply-to handling, channel guards | 04 |
| [07-install-ux](specs/07-install-ux/spec.md) | DRAFT | curl|bash installer, repo path resolver, install user journeys | 01, 03 |

## Dev Conventions

- **Python 3.12+** (upstream uses PEP 695 syntax in `agent.py`). Dependencies in `requirements.txt`; pin overrides in `requirements2.txt`; dev tools in `pyproject.toml`'s `[project.optional-dependencies] dev`.
- **Extensions over core edits.** Prefer writing extension files in `python/extensions/<hook>/` over modifying `agent.py` or `models.py`. New behavior goes in the `hyperagent0/` wrapper package (spec 01 D9), not in upstream-mirrored files.
- **Test command**: `python -m pytest tests/`. Our new tests live next to upstream's: `test_haz_setup_command.py`, `test_haz_check_command.py`, `test_wheel_contents.py`, etc.
- **CI**: `.github/workflows/install-smoke.yml` replays the full user journey (curl|bash install → daemon up → UI 200 → wheel-contents asserts) on every push/PR. Goes green in ~80s on a clean Ubuntu runner.
- **Commit format**: `<type>(<spec-name>): <description>` per spec-driven-dev convention. Types: `feat`, `fix`, `chore`, `docs`, `spec`, `refactor`, `test`.
- **Cold-start budget**: `haz --help`, `haz status`, `haz stop`, `haz check --help` MUST stay under 200ms (spec 03 D5). Heavy imports (LiteLLM, channel SDKs, Flask) go inside command bodies, never at module load.

## Proxy LLM (developer-only)

The user runs a local proxy LLM at `localhost:20128` (OpenAI-compatible, serves Claude models). To wire it into a dev install:

```bash
haz config set chat_model_provider openai
haz config set chat_model_name cc/claude-sonnet-4-6
haz config set chat_model_api_base http://localhost:20128
haz check   # should return "OK (Xs) — openai/cc/claude-sonnet-4-6 responded."
```

This is documentation for the maintainer's local setup, NOT something `install.sh` should write into a fresh install — end users configure the LLM via the web UI (spec 07 D2).
