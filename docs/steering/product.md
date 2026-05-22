# Product Vision: HyperAgent Zero

## What it is

A hyperagent harness — a host-native process that orchestrates AI agents, manages projects with isolated execution sandboxes, and is reachable from chat platforms (Slack, Telegram, Discord) as well as a web UI.

## Fork rationale

Agent Zero has the best foundation among evaluated frameworks (NanoClaw, OpenHarness, Agent Zero):
- **Extension system** (`@extensible` decorator) allows non-invasive modification of every lifecycle hook
- **Project model** (`.a0proj/`) is the only framework with first-class project concept
- **LLM flexibility** (LiteLLM) supports any provider including local proxy
- **MCP + SKILL.md** already implemented (Anthropic open standards)

What Agent Zero lacks:
- Host-first architecture (assumes Docker-containerized deployment)
- Claude Agent SDK as a provider (only has LiteLLM chat completions)
- Daemon lifecycle (no start/stop/restart, no systemd)
- Chat channel integration (web UI only)
- Per-project execution isolation (projects share filesystem and shell sessions)

## Who it's for

A developer/power user who wants a persistent AI agent managing multiple projects on their machine, reachable from chat apps, with each project sandboxed so code execution in one project can't affect another.

## Success criteria

1. `pip install hyperagent-zero && hyperagent-zero start` works on a fresh Linux/macOS machine with no Docker
2. Agent responds to messages from Telegram and Slack, not just the web UI
3. Claude Agent SDK with extended thinking works as a provider alongside the existing LiteLLM path
4. `hyperagent-zero status` shows running projects, active agents, resource usage
