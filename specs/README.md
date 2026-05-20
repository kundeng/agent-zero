# HyperAgent Zero — Spec Index

Fork of [agent0ai/agent-zero](https://github.com/agent0ai/agent-zero) with five key improvements to turn it into a proper hyperagent harness.

## Specs

| # | Spec | Status | Priority | Breaks Compat? |
|---|------|--------|----------|----------------|
| 1 | [Host-First Architecture](001-host-first-architecture.md) | draft | P0 | Yes (startup path) |
| 2 | [Claude Agent SDK Provider](002-claude-agent-sdk-provider.md) | draft | P0 | No (additive) |
| 3 | [Daemon & CLI](003-daemon-cli.md) | draft | P1 | No (additive) |
| 4 | [Chat Channel Integration](004-chat-channels.md) | draft | P2 | Yes (comms layer) |
| 5 | [Project Isolation & Sandboxing](005-project-isolation.md) | draft | P1 | Yes (execution model) |

## Branch Strategy

- `main` — tracks upstream agent0ai/agent-zero
- `v2-hyperagent` — our fork with these specs implemented
- Cherry-pick upstream fixes from main → v2-hyperagent as needed

## What Agent Zero Already Has (no spec needed)

These were initially flagged as gaps but the code review found them present:

- **MCP client support**: `python/helpers/mcp_handler.py` — full MCP client with stdio, SSE, and streamable-http transports. Tools from MCP servers surface as agent tools.
- **SKILL.md standard format**: `python/helpers/skills.py` + `python/tools/skills_tool.py` — Anthropic open-standard SKILL.md with frontmatter (name, description, version, author, tags, triggers, allowed_tools). Skills loaded from `skills/`, `usr/skills/`, and per-project/per-agent paths.
- **Proxy LLM support**: LiteLLM supports `api_base` override — setting `chat_model_provider=openai` + `chat_model_api_base=http://localhost:8317` works today with no code changes.
- **Project model**: `.a0proj/` with project.json, instructions/, knowledge/, variables.env, secrets/, agents.json, per-project extensions.

## Architecture Principles

1. **Agent runs on host, code runs in sandbox.** The harness process is never containerized.
2. **Standard protocols.** MCP for tools, SKILL.md for skills, OpenAI-compatible for LLM proxy.
3. **Per-project isolation.** Each project gets its own sandbox (Docker/cgroup), its own file mount, its own resource limits.
4. **Idempotent operations.** Start, stop, install — all safe to run repeatedly.
5. **Chat-native.** Reachable from Slack, Telegram, Discord, not just the web UI.

## Implementation Order

```
Spec 001 (Host-First)  ──→  Spec 003 (Daemon/CLI)  ──→  Spec 004 (Chat Channels)
         │                                                          
         └──────────────→  Spec 005 (Project Isolation)
         
Spec 002 (Claude SDK)  ──→  (independent, can be done in parallel)
```

P0 first pass: 001 + 002 (enable host-first + Claude SDK)
P1 second pass: 003 + 005 (daemon lifecycle + project sandboxing)
P2 third pass: 004 (chat channels)
