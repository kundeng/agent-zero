"""ClaudeSDKWrapper — Claude provider via local CLI auth (spec 02 D1 reframed).

Per project memory 2026-05-22 ("spec 02 SDK-only via local creds"), this
wrapper uses the official ``claude-agent-sdk`` package which spawns the
``claude`` CLI as a subprocess and delegates authentication to the CLI's
existing login (Pro / Max subscription). **No ``ANTHROPIC_API_KEY`` is
required** — this is the whole point: a user with a Claude subscription
can run the agent without paying per-token API rates.

Two distinct Anthropic Python SDKs share similar names; only one belongs
here:

* ``anthropic`` (API-key path) — what an earlier draft of this file used.
  Removed because it requires a metered API key.
* ``claude-agent-sdk`` (CLI subprocess) — what we use now. Delegates auth
  to the locally-installed ``claude`` CLI.

Selection: ``chat_model_provider == "claude-sdk"`` in settings dispatches
through ``models.get_chat_model`` → here.

Settings hooks (read by ``models.get_chat_model`` if not overridden):

    claude_sdk_model           default ``claude-sonnet-4-5``
    claude_sdk_cli_path        optional explicit ``claude`` binary path
    claude_sdk_thinking_budget extended thinking tokens; 0 disables
    claude_sdk_max_turns       agentic depth; ``1`` = pure completion

Public surface mirrors ``LiteLLMChatWrapper``: the agent monologue loop only
calls ``unified_call``; ``_astream`` is the streaming variant some Tools use.
``anthropic`` is **never** imported from this module.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, List, Optional, Tuple

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage


_DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"


class ClaudeSDKWrapper:
    """Drop-in replacement for ``LiteLLMChatWrapper`` when provider=claude-sdk.

    Uses ``claude_agent_sdk.query()`` which runs the ``claude`` CLI as a
    subprocess and yields typed messages (``AssistantMessage`` /
    ``ResultMessage`` / etc.). Auth flows through whatever account the CLI
    is logged into — typically the user's Pro / Max subscription.
    """

    provider: str = "claude-sdk"

    def __init__(
        self,
        model: str,
        provider: str = "claude-sdk",
        model_config: Optional[Any] = None,
        **kwargs: Any,
    ):
        # Lazy import keeps ``haz --help`` and the base wheel free of the
        # [claude-sdk] extra. Failure here surfaces as a clear install hint
        # at provider-selection time, not at module-load time.
        try:
            import claude_agent_sdk  # type: ignore
        except ImportError as e:  # pragma: no cover - import-time signal only
            raise ImportError(
                "chat_model_provider=claude-sdk requires the optional extra. "
                "Install: pip install hyperagent0[claude-sdk] "
                "(or directly: pip install claude-agent-sdk)."
            ) from e

        self._sdk = claude_agent_sdk
        self.model_name = model or _DEFAULT_CLAUDE_MODEL
        self.provider = provider or "claude-sdk"
        self.kwargs = dict(kwargs)

        # API key not consumed — claude_agent_sdk auths through the CLI.
        # We accept and discard it so a settings page that still carries
        # the key doesn't blow up.
        self.kwargs.pop("api_key", None)

        self.cli_path: Optional[str] = self.kwargs.pop("cli_path", None) or None
        self.thinking_budget = int(self.kwargs.pop("thinking_budget", 0) or 0)
        # max_turns=1 keeps the SDK in pure-completion mode. Agent Zero's
        # monologue loop is what drives the multi-turn behavior; letting the
        # SDK also iterate would double-loop.
        self.max_turns = int(self.kwargs.pop("max_turns", 1) or 1)
        self.a0_model_conf = model_config

    @property
    def _llm_type(self) -> str:
        return "claude-sdk-cli"

    # ------------------------------------------------------------------
    # Message conversion
    # ------------------------------------------------------------------

    def _convert_messages(
        self, messages: List[BaseMessage]
    ) -> tuple[str, str]:
        """Flatten LangChain messages into (system_prompt, user_prompt).

        ``claude_agent_sdk.query()`` takes a single ``prompt`` string plus
        an optional ``system_prompt`` in options. We concatenate all
        non-system turns into the user prompt — Agent Zero's monologue
        loop typically calls us with one system + one user message, so
        the flattening is a no-op in the common case.
        """
        system_chunks: list[str] = []
        user_chunks: list[str] = []
        for m in messages:
            content = m.content if isinstance(m.content, str) else str(m.content)
            if m.type == "system":
                if content:
                    system_chunks.append(content)
                continue
            # human / ai / tool — flatten to the prompt side. The CLI sees
            # one consolidated turn; thinking/tool semantics handled by the
            # outer Agent loop.
            if content:
                user_chunks.append(content)
        return "\n\n".join(system_chunks), "\n\n".join(user_chunks)

    # ------------------------------------------------------------------
    # unified_call — primary entrypoint used by Agent.call_chat_model
    # ------------------------------------------------------------------

    async def unified_call(
        self,
        system_message: str = "",
        user_message: str = "",
        messages: List[BaseMessage] | None = None,
        response_callback: Callable[[str, str], Awaitable[None]] | None = None,
        reasoning_callback: Callable[[str, str], Awaitable[None]] | None = None,
        tokens_callback: Callable[[str, int], Awaitable[None]] | None = None,
        rate_limiter_callback: (
            Callable[[str, str, int, int], Awaitable[bool]] | None
        ) = None,
        explicit_caching: bool = False,
        **kwargs: Any,
    ) -> Tuple[str, str]:
        msgs: list[BaseMessage] = list(messages) if messages else []
        if system_message:
            msgs.insert(0, SystemMessage(content=system_message))
        if user_message:
            msgs.append(HumanMessage(content=user_message))

        system_text, user_text = self._convert_messages(msgs)

        options = self._build_options(system_text=system_text)

        response_text = ""
        reasoning_text = ""

        async for sdk_msg in self._sdk.query(prompt=user_text, options=options):
            for r_delta, t_delta in self._iter_block_deltas(sdk_msg):
                if t_delta:
                    reasoning_text += t_delta
                    if reasoning_callback:
                        await reasoning_callback(t_delta, reasoning_text)
                    if tokens_callback:
                        await tokens_callback(t_delta, _approx_tokens(t_delta))
                if r_delta:
                    response_text += r_delta
                    if response_callback:
                        await response_callback(r_delta, response_text)
                    if tokens_callback:
                        await tokens_callback(r_delta, _approx_tokens(r_delta))

        return response_text, reasoning_text

    # ------------------------------------------------------------------
    # _astream — parity with LiteLLMChatWrapper._astream
    # ------------------------------------------------------------------

    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        system_text, user_text = self._convert_messages(messages)
        options = self._build_options(system_text=system_text)
        async for sdk_msg in self._sdk.query(prompt=user_text, options=options):
            for r_delta, _ in self._iter_block_deltas(sdk_msg):
                if r_delta:
                    yield r_delta

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_options(self, *, system_text: str) -> Any:
        """Construct ``ClaudeAgentOptions`` from this wrapper's settings.

        ``allowed_tools=[]`` is the critical guardrail: Agent Zero's own
        tools (code_execution, call_subordinate, etc.) are dispatched by
        the outer monologue loop, not by the SDK's tool-use machinery. We
        only want the LLM to generate text — the SDK becomes a pure
        completion endpoint.
        """
        opts_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "max_turns": self.max_turns,
            "allowed_tools": [],
            "permission_mode": "default",
        }
        if system_text:
            opts_kwargs["system_prompt"] = system_text
        if self.cli_path:
            opts_kwargs["cli_path"] = self.cli_path
        if self.thinking_budget > 0:
            # Both budget and thinking-config are surfaced so the CLI sees
            # an intent regardless of which key version it honors.
            opts_kwargs["max_thinking_tokens"] = self.thinking_budget
            try:
                opts_kwargs["thinking"] = self._sdk.ThinkingConfigEnabled(
                    type="enabled", budget_tokens=self.thinking_budget
                )
            except AttributeError:
                pass
        return self._sdk.ClaudeAgentOptions(**opts_kwargs)

    def _iter_block_deltas(self, sdk_msg: Any) -> list[tuple[str, str]]:
        """Yield ``(response_delta, thinking_delta)`` for each content block.

        Only ``AssistantMessage`` carries content blocks. Other message
        types (``UserMessage`` / ``ResultMessage`` / ``RateLimitEvent`` /
        ``SystemMessage`` / ``HookEventMessage``) are emitted by the SDK
        for state-of-the-CLI bookkeeping and yield no deltas.

        Per-block iteration preserves the streaming granularity the
        agent's UI expects — each ``TextBlock`` becomes one delta, each
        ``ThinkingBlock`` one reasoning chunk. ``ToolUseBlock`` and
        other block types are skipped: ``allowed_tools=[]`` in our
        options keeps the SDK's tool path inert, so anything we see is
        an upstream surprise we don't want to render as text.
        """
        if not isinstance(sdk_msg, self._sdk.AssistantMessage):
            return []
        out: list[tuple[str, str]] = []
        for block in getattr(sdk_msg, "content", None) or []:
            if isinstance(block, self._sdk.TextBlock):
                text = getattr(block, "text", "") or ""
                if text:
                    out.append((text, ""))
            elif isinstance(block, self._sdk.ThinkingBlock):
                thought = getattr(block, "thinking", "") or ""
                if thought:
                    out.append(("", thought))
        return out


def _approx_tokens(text: str) -> int:
    """Lightweight token estimate.

    The accurate ``approximate_tokens`` lives in ``python.helpers.tokens``
    but importing it here would create a cycle on first provider selection
    in some contexts.
    """
    return max(1, len(text) // 4)
