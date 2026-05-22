# Tech Decisions: HyperAgent Zero

## Inherited stack

- **Language**: Python 3.11+
- **Web framework**: Flask + Starlette + Socket.IO via uvicorn
- **LLM gateway**: LiteLLM (universal provider)
- **MCP**: `mcp` Python package (stdio, SSE, streamable-http)
- **Search**: DuckDuckGo, FAISS for embeddings
- **Browser**: Playwright via browser-use
- **SSH**: paramiko (for Docker code execution)

## New decisions (v2-hyperagent)

### D1: Host-first, not Docker-first
**Choice**: Agent process runs natively on host. Docker is opt-in for code execution sandboxing only.
**Why**: The agent needs host access (filesystem, network, process management, credential stores). Containerizing the agent prevents composition with host services. The code already supports this (`ssh_enabled=false`, `LocalInteractiveSession`), we're making it the default.

### D2: Claude Agent SDK alongside LiteLLM
**Choice**: Add `ClaudeSDKWrapper` as an alternative to `LiteLLMChatWrapper`. User picks via `chat_model_provider=claude-sdk`.
**Why**: LiteLLM gives raw chat completions. Claude SDK gives extended thinking, native tool use, streaming tool results, and built-in MCP. Both paths coexist — LiteLLM for proxy/non-Claude, SDK for direct Claude.

### D3: ~~cgroup v2 as primary sandbox on Linux~~ — WITHDRAWN 2026-05-22
Spec 05 (project-isolation) was retracted. Sandbox mode is a single global setting (`none` / `sandbox` / `ssh`) inherited from the agent's deployment environment. No per-project containers, no cgroup/docker/podman backends. See `specs/05-project-isolation/spec.md` for the WITHDRAWN notice.

### D4: Click-based CLI
**Choice**: `click` for CLI (hyperagent-zero start/stop/status/etc).
**Why**: Already in wide use, supports subcommands, help generation, and argument validation. Lightweight dependency.

### D5: Channel adapters as Python async classes
**Choice**: Build channel adapters in Python (not bridge to NanoClaw's TypeScript Chat SDK).
**Why**: Avoids Node.js sidecar dependency. Three initial adapters (Telegram, Slack, Discord) are straightforward with existing Python libraries (python-telegram-bot, slack-bolt, discord.py). Consider NanoClaw Chat SDK bridge as Phase 2 stretch goal for the remaining 20+ platforms.
