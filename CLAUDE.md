# HyperAgent Zero

Fork of [agent0ai/agent-zero](https://github.com/agent0ai/agent-zero) — a personal agentic framework — enhanced into a hyperagent harness with host-first architecture, project isolation, Claude Agent SDK support, daemon lifecycle, and chat channel integration.

specs_root: specs/

## Branch Strategy

- `main` — tracks upstream `agent0ai/agent-zero`. Never commit directly.
- `v2-hyperagent` — our development branch. All specs implemented here.
- Cherry-pick upstream fixes: `git cherry-pick <sha>` from main into v2-hyperagent.

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
`python/tools/code_execution_tool.py` — dispatches to `LocalInteractiveSession` (host PTY) or `SSHInteractiveSession` (Docker via paramiko). Controlled by `ssh_enabled` setting.

### MCP Support
`python/helpers/mcp_handler.py` — full MCP client: stdio, SSE, streamable-http transports. MCP tools surface as agent tools.

### Skills
`python/helpers/skills.py` — Anthropic open-standard SKILL.md with frontmatter. Loaded from `skills/`, `usr/skills/`, per-project, per-agent paths. `python/tools/skills_tool.py` exposes list/load methods.

### Sub-agents
`python/tools/call_subordinate.py` — spawns new `Agent` with `number+1`. **Blocking**: `await subordinate.monologue()`. Profiles in `agents/<profile>/`.

### Settings
`python/helpers/settings.py` — all config. Key fields: `chat_model_provider`, `chat_model_name`, `chat_model_api_base`, `ssh_enabled`, `rfc_auto_docker`, `rfc_url`.

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
| `python/tools/code_execution_tool.py` | Code exec: local PTY or SSH to Docker |
| `python/tools/call_subordinate.py` | Sub-agent spawning (blocking) |
| `python/tools/skills_tool.py` | Skill list/load tool |

## What We're Building (v2-hyperagent specs)

| Spec | Status | What it does | Depends on |
|------|--------|-------------|------------|
| [01-host-first](specs/01-host-first/spec.md) | DRAFT | Agent runs on host, sandbox only for code exec | — |
| [02-claude-sdk](specs/02-claude-sdk/spec.md) | DRAFT | Claude Agent SDK as first-class provider alongside LiteLLM | — |
| [03-daemon-cli](specs/03-daemon-cli/spec.md) | DRAFT | `hyperagent-zero start/stop/status`, systemd, pip install | 01 |
| [04-chat-channels](specs/04-chat-channels/spec.md) | DRAFT | Telegram, Slack, Discord channel adapters | 01, 03 |
| [05-project-isolation](specs/05-project-isolation/spec.md) | DRAFT | Per-project cgroup/Docker sandboxing with resource limits | 01 |

## Dev Conventions

- **Python 3.11+**. Dependencies in `requirements.txt`.
- **Extensions over core edits.** Prefer writing extension files in `python/extensions/<hook>/` over modifying `agent.py` or `models.py`.
- **Test command**: `python -m pytest tests/` (limited upstream tests exist)
- **Commit format**: `feat(<spec>/<task>): [description]` per spec-driven-dev convention

## Proxy LLM

The user has a proxy LLM at `localhost:8317` (OpenAI-compatible, serves Claude models). To use it:
```json
{
    "chat_model_provider": "openai",
    "chat_model_name": "claude-sonnet-4-20250514",
    "chat_model_api_base": "http://localhost:8317"
}
```
