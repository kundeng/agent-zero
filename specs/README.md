# HyperAgent Zero — Spec Index

Fork of [agent0ai/agent-zero](https://github.com/agent0ai/agent-zero) with five key improvements to turn it into a proper hyperagent harness.

## Specs

| # | Spec | Status | Epic | Depends On |
|---|------|--------|------|------------|
| 01 | [Host-First Architecture](01-host-first/spec.md) | DRAFT | architecture | — |
| 02 | [Claude Agent SDK Provider](02-claude-sdk/spec.md) | DRAFT | llm-providers | — |
| 03 | [Daemon & CLI](03-daemon-cli/spec.md) | DRAFT | devops | 01-host-first |
| 04 | [Chat Channel Integration](04-chat-channels/spec.md) | DRAFT | channels | 01-host-first, 03-daemon-cli |
| 05 | [Project Isolation & Sandboxing](05-project-isolation/spec.md) | DRAFT | architecture | 01-host-first |

## What Agent Zero Already Has (no spec needed)

- **MCP client support**: `python/helpers/mcp_handler.py` — stdio, SSE, streamable-http transports
- **SKILL.md standard format**: `python/helpers/skills.py` — Anthropic open-standard with frontmatter
- **Proxy LLM support**: LiteLLM `api_base` override → `localhost:8317` works today
- **Project model**: `.a0proj/` with project.json, instructions, knowledge, secrets, agent overrides

## Implementation Order

```
01-host-first  ──→  03-daemon-cli  ──→  04-chat-channels
      │                                          
      └──────────→  05-project-isolation

02-claude-sdk  ──→  (independent, parallel with 01)
```

P0 first pass: 01 + 02 (host-first + Claude SDK)
P1 second pass: 03 + 05 (daemon lifecycle + project sandboxing)
P2 third pass: 04 (chat channels)
