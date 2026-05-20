---
title: "Claude Agent SDK Provider"
status: draft
priority: P0
breaks_compat: false
depends_on: []
---

# Spec 002: Claude Agent SDK Provider

## Problem

Agent Zero uses LiteLLM as its universal LLM gateway, which supports Claude via the Anthropic API. But this gives you raw chat completions — not the Claude Agent SDK's richer capabilities:

- **Extended thinking** (thinking blocks with budget control)
- **Native tool use** with structured tool definitions and results
- **Streaming tool results** (the agent sees tool output as it streams)
- **Session management** with conversation compaction
- **MCP server integration** built into the SDK

The user also has a proxy LLM at `localhost:8317` (OpenAI-compatible) that can serve Claude models. Both paths should work.

## Requirements

### R1: Claude Agent SDK as a first-class provider
- New provider type `claude-sdk` alongside existing LiteLLM providers
- Uses `@anthropic-ai/claude-code` or `anthropic` Python SDK directly
- Supports extended thinking, native tool use, streaming
- Configurable in settings just like other model providers

### R2: Proxy LLM continues to work
- The existing LiteLLM path with `api_base=http://localhost:8317` remains functional
- No changes to existing provider configuration
- Users choose: Claude SDK (native) vs. proxy (OpenAI-compatible) vs. LiteLLM (any provider)

### R3: Tool bridging
- Agent Zero's internal tools (code_execution, memory, browser, etc.) are exposed to Claude SDK as native tool definitions
- Claude SDK's tool_use responses are mapped back to Agent Zero's `Tool.execute()` dispatch
- MCP tools configured in Agent Zero are also available through the SDK's native MCP support

## Design

### Provider abstraction

Agent Zero's current model layer (`models.py`) wraps everything through LiteLLM:

```python
class LiteLLMChatWrapper:
    async def chat(self, messages, tools, ...) → response
```

We add a parallel path:

```python
class ClaudeSDKWrapper:
    async def chat(self, messages, tools, ...) → response
    # Internally uses anthropic.Anthropic() or anthropic.AsyncAnthropic()
    # Maps Agent Zero tool definitions → Claude tool schemas
    # Maps Claude tool_use blocks → Agent Zero tool dispatch
    # Supports extended thinking via thinking_budget parameter
```

### Key files to modify/create

| File | Change |
|------|--------|
| `models.py` | Add `ClaudeSDKWrapper` class, new provider type `claude-sdk` |
| `python/helpers/settings.py` | Add `claude_sdk_*` settings (api_key, thinking_budget, model) |
| `initialize.py` | Initialize Claude SDK provider when `chat_model_provider=claude-sdk` |
| NEW: `python/helpers/claude_sdk_bridge.py` | Tool schema translation, response mapping |
| `agent.py` | Ensure monologue loop handles thinking blocks in responses |

### Tool schema translation

Agent Zero tools are defined as Python classes with `Tool.execute()`. Claude SDK expects JSON schemas:

```python
# Agent Zero tool definition (current)
class CodeExecutionTool(Tool):
    async def execute(self, **kwargs) → Response:
        ...

# Claude SDK tool schema (generated)
{
    "name": "code_execution_tool",
    "description": "Execute code in a sandboxed environment",
    "input_schema": {
        "type": "object",
        "properties": {
            "runtime": {"type": "string", "enum": ["python", "bash", "node"]},
            "code": {"type": "string"}
        }
    }
}
```

The bridge auto-generates schemas from Tool class metadata and routes Claude's `tool_use` responses back to the appropriate `Tool.execute()`.

### Configuration

```json
{
    "chat_model_provider": "claude-sdk",
    "chat_model_name": "claude-sonnet-4-20250514",
    "chat_model_api_base": "",
    "claude_sdk_thinking_budget": 10000,
    "claude_sdk_api_key": "sk-ant-..."
}
```

Or via proxy:
```json
{
    "chat_model_provider": "openai",
    "chat_model_name": "claude-sonnet-4-20250514",
    "chat_model_api_base": "http://localhost:8317",
    "chat_model_api_key": "proxy-key"
}
```

## Risks

- **Tool schema drift**: If Agent Zero tools change their kwargs, the auto-generated schemas must update. Mitigated by generating schemas at runtime from Tool class introspection.
- **Thinking block handling**: The monologue loop currently expects `content` strings. Thinking blocks are a different content type. The loop needs to handle `thinking` → `text` → `tool_use` sequences without breaking.
- **Cost**: Claude SDK with extended thinking can be expensive. Need cost tracking integration (leverage existing rate limiter in `rate_limiter.py`).

## Tasks

- [ ] Add `anthropic` to requirements.txt
- [ ] Create `python/helpers/claude_sdk_bridge.py` — tool schema translator
- [ ] Create `ClaudeSDKWrapper` in `models.py`
- [ ] Add `claude-sdk` provider settings to `settings.py`
- [ ] Modify `agent.py` monologue loop to handle thinking blocks
- [ ] Wire MCP tools through Claude SDK's native MCP support
- [ ] Test: Claude SDK provider with direct Anthropic API
- [ ] Test: LiteLLM provider with proxy at localhost:8317
- [ ] Test: Tool dispatch round-trip (Claude calls tool → Agent Zero executes → result back)
- [ ] Test: Extended thinking with budget control
