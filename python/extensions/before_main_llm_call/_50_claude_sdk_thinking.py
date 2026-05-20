"""Spec 02-claude-sdk task 1.5 — set up per-iteration state for Claude thinking blocks.

This extension fires before each main LLM call. When the active chat-model
provider is ``claude-sdk``, it stamps the loop_data with the configured thinking
budget so downstream UI hooks (and the process_chain_end logger) can read it.

The actual ``thinking={"type": "enabled", "budget_tokens": N}`` flag is applied
inside ``ClaudeSDKWrapper`` (from ``settings.claude_sdk_thinking_budget``); this
extension exists so the budget can be inspected, surfaced in the log, and is a
natural extension point for per-profile budget overrides in the future.
"""

from __future__ import annotations

from agent import LoopData
from python.helpers import settings
from python.helpers.extension import Extension


class ClaudeSDKThinkingPrep(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs) -> None:
        try:
            current = settings.get_settings()
        except Exception:
            return

        if current.get("chat_model_provider", "").lower() != "claude-sdk":
            return

        budget = int(current.get("claude_sdk_thinking_budget", 0) or 0)
        loop_data.params_temporary["claude_sdk_thinking_budget"] = budget
        # Reset the per-iteration thinking accumulator; the reasoning_callback
        # path in ClaudeSDKWrapper streams thinking deltas through the existing
        # reasoning extension hooks, but we also keep a copy here for the
        # process_chain_end logger.
        loop_data.params_temporary.setdefault("claude_sdk_thinking_log", [])
