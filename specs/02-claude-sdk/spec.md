---
spec_id: 02-claude-sdk
status: DRAFT
since: 2026-05-20
until: null
epic: llm-providers
features: [claude-sdk-provider, tool-schema-bridge, thinking-blocks]
supersedes: []
superseded_by: null
depends_on: []
---

# Claude Agent SDK Provider

## Context

Agent Zero uses LiteLLM as its universal LLM gateway, which supports Claude via the Anthropic API. But this gives raw chat completions — not the Claude Agent SDK's richer capabilities:

- **Extended thinking** (thinking blocks with budget control)
- **Native tool use** with structured tool definitions and results
- **Streaming tool results** (the agent sees tool output as it streams)
- **Session management** with conversation compaction
- **MCP server integration** built into the SDK

The user also has a proxy LLM at `localhost:8317` (OpenAI-compatible) that can serve Claude models. Both paths must work.

## Constraints

- Must not break existing LiteLLM provider path
- `anthropic` Python SDK added to requirements.txt (not `@anthropic-ai/claude-code` which is Node.js)
- Tool schema auto-generated at runtime from Tool class introspection — no manual schema maintenance
- Extended thinking budget must be configurable and cost-trackable

## Decisions

### D1: Parallel wrapper, not replacement
**Choice**: Add `ClaudeSDKWrapper` alongside `LiteLLMChatWrapper` in `models.py`. Selection via `chat_model_provider=claude-sdk`.
**Why**: LiteLLM handles 100+ providers. Claude SDK handles Claude-specific features. Both coexist.

### D2: Auto-generate tool schemas from Tool classes
**Choice**: Introspect `Tool` subclasses at runtime to generate Claude-format JSON schemas.
**Why**: Avoids manual schema maintenance. If a tool's kwargs change, the schema updates automatically.

### D3: Thinking blocks handled in monologue loop
**Choice**: Modify `agent.py` monologue loop to handle `thinking` → `text` → `tool_use` content sequences.
**Why**: Thinking blocks are a different content type. The loop currently expects string content only. Minimal change: treat thinking blocks as internal context, pass text and tool_use to existing dispatch.

## Tasks

### P1 — Must Do
- [ ] 1.1 Add `anthropic` to requirements.txt
- [ ] 1.2 Create `python/helpers/claude_sdk_bridge.py`
  - Tool schema translator: Tool class → Claude JSON schema
  - Response mapper: Claude tool_use blocks → Agent Zero tool dispatch
  - Thinking block extractor
- [ ] 1.3 Create `ClaudeSDKWrapper` in `models.py`
  - Implement same interface as `LiteLLMChatWrapper`
  - Use `anthropic.AsyncAnthropic()` for streaming
  - Map Agent Zero messages format to/from Claude Messages API format
  - [src:models.py]
- [ ] 1.4 Add `claude-sdk` provider settings to `settings.py`
  - `claude_sdk_thinking_budget`, `claude_sdk_api_key`, `claude_sdk_model`
  - [src:python/helpers/settings.py]
- [ ] 1.5 Modify `agent.py` monologue loop to handle thinking blocks
  - Detect thinking content type in response
  - Log thinking blocks (visible in UI but not injected back as user content)
  - Pass text + tool_use to existing processing
  - [src:agent.py]
- [ ] 1.6 Wire MCP tools through Claude SDK's native MCP support
  - Bridge Agent Zero MCP handler → Claude SDK's tool definitions
  - [src:python/helpers/mcp_handler.py]

### P2 — Should Do
- [ ] 2.1 Test: Claude SDK provider with direct Anthropic API key
- [ ] 2.2 Test: LiteLLM provider with proxy at localhost:8317 (unchanged)
- [ ] 2.3 Test: Tool dispatch round-trip (Claude calls tool → execute → result back)
- [ ] 2.4 Test: Extended thinking with budget control
- [ ] 2.5 Integrate cost tracking for Claude SDK (thinking tokens are expensive)
  - Extend `rate_limiter.py` with per-provider token accounting
  - [src:python/helpers/rate_limiter.py]

### P3 — Nice to Have
- [ ] 3.1 UI display for thinking blocks (collapsible in web UI)
- [ ] 3.2 Claude SDK streaming tool results (tool output appears as it generates)

## Open Questions

- [ ] Should thinking budget be global or per-agent-profile? Likely per-profile for different task types.
- [ ] How to handle Claude SDK's built-in conversation compaction vs Agent Zero's own context management? May conflict.

## Log

**2026-05-20** — Initial spec. Confirmed LiteLLM is the only provider path today (`models.py:LiteLLMChatWrapper`). `mcp` package already in requirements.txt. `anthropic` package needs to be added.
