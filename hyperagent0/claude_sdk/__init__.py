"""Claude Agent SDK provider for HyperAgent Zero (spec 02-claude-sdk).

Net-new code per spec 01 D9. The `anthropic` package is only imported when
`chat_model_provider == "claude-sdk"` is actively selected.

Submodules:
    bridge   — Tool schema translator, response/thinking-block extractor.
    wrapper  — ClaudeSDKWrapper, parallel to LiteLLMChatWrapper in models.py.
    mcp      — Claude SDK native MCP wiring.
    cost     — Per-provider token accounting (P2).
"""
