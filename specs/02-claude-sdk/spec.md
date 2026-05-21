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

The user also has a proxy LLM at `localhost:20128` (OpenAI-compatible) that can serve Claude models. Both paths must work.

### Relationship to spec 01 (wrapper architecture)

Per spec 01 D9, all net-new code lives under `hyperagent0/`. This spec's home is `hyperagent0/claude_sdk/`:

```
hyperagent0/claude_sdk/
├── __init__.py
├── wrapper.py          # ClaudeSDKWrapper (same interface as LiteLLMChatWrapper)
├── bridge.py           # Tool schema translator, response mapper, thinking-block extractor
├── mcp.py              # MCP wiring (Claude SDK native MCP support → Agent Zero MCP handler)
└── cost.py             # Per-provider token accounting (P2)
```

**Conflict-surface budget for spec 02**: this spec touches more upstream files than spec 01 because Claude SDK has to plug into the agent loop and the model layer. Patched upstream files:

- `models.py` — register `ClaudeSDKWrapper` as a `chat_model_provider="claude-sdk"` option (small dispatch addition)
- `agent.py` — handle `thinking` content blocks in the monologue loop (behavior change at an existing call site)
- `python/helpers/settings.py` — add Claude SDK settings fields (also touched by spec 01 — same patch site)
- `python/helpers/mcp_handler.py` — bridge to Claude SDK's native MCP (P1 task 1.6)
- `python/helpers/rate_limiter.py` — per-provider token accounting (P2)

Where extensions can replace patches, prefer extensions per CLAUDE.md ("Extensions over core edits") — task 1.5 should investigate whether the monologue-loop thinking-block handling can be implemented as a `python/extensions/before_main_llm_call/` or `process_chain_end/` extension instead of an in-place patch.

## Constraints

- Must not break existing LiteLLM provider path
- `anthropic` (and `claude-agent-sdk` if used) installed via the `[claude-sdk]` extra per spec 01 D7 — **not** added to base `requirements.txt`. Users opt in: `pip install hyperagent0[claude-sdk]`.
- Tool schema auto-generated at runtime from Tool class introspection — no manual schema maintenance
- Extended thinking budget must be configurable and cost-trackable
- Lazy import: `import anthropic` happens only when `chat_model_provider=claude-sdk` is selected; the base wheel must install and `haz --help` must run without the extra installed

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
- [ ] 1.1 Add `[claude-sdk]` extra to `pyproject.toml`
  - `[claude-sdk] = ["anthropic>=0.40", "claude-agent-sdk"]` (versions pinned at implementation time)
  - Confirm base install + `haz --help` work without the extra installed
  - [src:pyproject.toml]
- [ ] 1.2 Create `hyperagent0/claude_sdk/bridge.py`
  - Tool schema translator: `Tool` class → Claude JSON schema (via runtime introspection of kwargs)
  - Response mapper: Claude `tool_use` blocks → Agent Zero tool dispatch
  - Thinking-block extractor (returns `(thinking, text, tool_uses)` tuples)
- [ ] 1.3 Create `hyperagent0/claude_sdk/wrapper.py` (`ClaudeSDKWrapper` class)
  - Implements the same interface as `LiteLLMChatWrapper`
  - Uses `anthropic.AsyncAnthropic()` for streaming (lazy import inside `__init__`)
  - Maps Agent Zero messages format to/from Claude Messages API format
  - Register in `models.py` with a minimal dispatch patch: when `chat_model_provider == "claude-sdk"`, instantiate `hyperagent0.claude_sdk.wrapper.ClaudeSDKWrapper`
  - [src:models.py, hyperagent0/claude_sdk/wrapper.py]
- [ ] 1.4 Add Claude SDK provider settings
  - Fields: `claude_sdk_thinking_budget`, `claude_sdk_api_key`, `claude_sdk_model`
  - Co-located with spec 01's `sandbox_mode` field addition in `python/helpers/settings.py` (same patch site, ideally same PR if specs 01 and 02 land together)
  - [src:python/helpers/settings.py]
- [ ] 1.5 Handle thinking blocks in the agent monologue
  - **First preference: extension-based.** Try implementing thinking-block handling as a `python/extensions/before_main_llm_call/` + `process_chain_end/` pair, registering through `python/helpers/extension.py`'s `@extensible` framework.
  - **Fallback: patch `agent.py` monologue loop.** Only if the extension hooks don't expose enough state. Detect thinking content type in response, log thinking blocks (visible in UI but not injected back as user content), pass text + tool_use to existing processing.
  - Document the choice in the implementation PR.
  - [src:python/extensions/ OR agent.py]
- [ ] 1.6 Wire MCP tools through Claude SDK's native MCP support
  - Bridge logic in `hyperagent0/claude_sdk/mcp.py`
  - Minimal patch to `python/helpers/mcp_handler.py`: when active provider is `claude-sdk`, route tool registration through the bridge module
  - [src:python/helpers/mcp_handler.py, hyperagent0/claude_sdk/mcp.py]

### P2 — Should Do
- [ ] 2.1 Test: Claude SDK provider with direct Anthropic API key
- [ ] 2.2 Test: LiteLLM provider with proxy at localhost:20128 (unchanged)
- [ ] 2.3 Test: Tool dispatch round-trip (Claude calls tool → execute → result back)
- [ ] 2.4 Test: Extended thinking with budget control
- [ ] 2.5 Integrate cost tracking for Claude SDK (thinking tokens are expensive)
  - `hyperagent0/claude_sdk/cost.py` implements per-provider token accounting
  - Minimal patch to `python/helpers/rate_limiter.py`: call out to the cost module for claude-sdk provider
  - [src:python/helpers/rate_limiter.py, hyperagent0/claude_sdk/cost.py]

### P3 — Nice to Have
- [ ] 3.1 UI display for thinking blocks (collapsible in web UI)
- [ ] 3.2 Claude SDK streaming tool results (tool output appears as it generates)

## Open Questions

- [ ] Should thinking budget be global or per-agent-profile? Likely per-profile for different task types.
- [ ] How to handle Claude SDK's built-in conversation compaction vs Agent Zero's own context management? May conflict.

## Log

**2026-05-20** — Initial spec. Confirmed LiteLLM is the only provider path today (`models.py:LiteLLMChatWrapper`). `mcp` package already in requirements.txt. `anthropic` package needs to be added.

**2026-05-20** — Aligned with spec 01 D9 wrapper architecture. All net-new code moves under `hyperagent0/claude_sdk/` (wrapper, bridge, mcp, cost). `anthropic` is now a `[claude-sdk]` extra (per spec 01 D7), not a base requirement — keeps `pip install hyperagent0` lean. Task 1.5 (thinking blocks) updated to prefer extension-based implementation over an `agent.py` patch, per CLAUDE.md convention. Documented the spec-02 conflict-surface budget: 4 upstream files patched (`models.py`, `agent.py`, `settings.py` (shared with spec 01), `mcp_handler.py`), one more in P2 (`rate_limiter.py`).
