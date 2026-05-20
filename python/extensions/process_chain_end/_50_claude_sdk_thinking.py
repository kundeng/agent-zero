"""Spec 02-claude-sdk task 1.5 — log thinking blocks at end of process chain.

When the active provider is ``claude-sdk`` and a thinking budget was used, log a
one-line summary so operators can see thinking activity in the UI without the
raw thoughts being re-injected into the assistant turn (per spec D3: "treat
thinking blocks as internal context, pass text and tool_use to existing
dispatch").
"""

from __future__ import annotations

from agent import LoopData
from python.helpers import settings
from python.helpers.extension import Extension


class ClaudeSDKThinkingLog(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs) -> None:
        try:
            current = settings.get_settings()
        except Exception:
            return

        if current.get("chat_model_provider", "").lower() != "claude-sdk":
            return

        budget = int(loop_data.params_temporary.get("claude_sdk_thinking_budget", 0) or 0)
        if budget <= 0:
            return

        log = self.agent.context.log
        log.log(
            type="info",
            heading=f"{self.agent.agent_name}: Claude extended thinking",
            content=f"thinking_budget={budget} tokens (spec 02-claude-sdk)",
        )
