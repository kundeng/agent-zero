"""Claude Agent SDK chat wrapper (spec 02 task 1.3).

Mirrors the public interface of ``models.LiteLLMChatWrapper`` so the agent
monologue loop can swap providers transparently:

    - ``unified_call(...)``  — primary entrypoint used by ``Agent.call_chat_model``.
    - ``_astream``           — async streaming generator.
    - ``model_name`` / ``provider`` properties.

``anthropic`` is imported **lazily** inside ``__init__`` so the base wheel can
install and ``haz --help`` can run without the ``[claude-sdk]`` extra.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, List, Optional, Tuple

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from .bridge import extract_response_blocks


# Default model — also exposed via settings.claude_sdk_model.
_DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"


class ClaudeSDKWrapper:
    """Drop-in replacement for ``LiteLLMChatWrapper`` when provider=claude-sdk.

    The wrapper intentionally does not subclass ``SimpleChatModel`` — Agent Zero
    only calls ``unified_call`` and the streaming helpers, and avoiding the
    LangChain base keeps the ``anthropic`` dependency surface clean.
    """

    provider: str = "claude-sdk"

    def __init__(
        self,
        model: str,
        provider: str = "claude-sdk",
        model_config: Optional[Any] = None,
        **kwargs: Any,
    ):
        # Lazy import: keep `anthropic` out of the base install path.
        try:
            import anthropic  # type: ignore
        except ImportError as e:  # pragma: no cover - import-time signal only
            raise ImportError(
                "Claude SDK provider selected but the `anthropic` package is not "
                "installed. Install the optional extra: `pip install hyperagent0[claude-sdk]`."
            ) from e

        self._anthropic = anthropic
        self.model_name = model or _DEFAULT_CLAUDE_MODEL
        self.provider = provider or "claude-sdk"
        # Strip Agent-Zero-only kwargs that the Anthropic SDK does not know about.
        self.kwargs = dict(kwargs)
        api_key = self.kwargs.pop("api_key", None) or None
        self.thinking_budget = int(self.kwargs.pop("thinking_budget", 0) or 0)
        self.a0_model_conf = model_config

        # AsyncAnthropic transparently supports streaming.
        self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()

    # ------------------------------------------------------------------
    # Message-format mapping
    # ------------------------------------------------------------------

    def _convert_messages(
        self, messages: List[BaseMessage]
    ) -> tuple[str, list[dict[str, Any]]]:
        """LangChain messages -> (system_text, anthropic_messages).

        Claude's Messages API takes ``system`` as a top-level argument and a
        ``messages`` list with only user/assistant turns.
        """
        system_chunks: list[str] = []
        anthropic_messages: list[dict[str, Any]] = []
        role_map = {"human": "user", "ai": "assistant", "system": "system", "tool": "user"}

        for m in messages:
            role = role_map.get(m.type, m.type)
            content = m.content if isinstance(m.content, str) else str(m.content)
            if role == "system":
                if content:
                    system_chunks.append(content)
                continue
            if role == "tool":
                # Tool result is just user content for Claude when we are running
                # the text-mode loop. (Native tool_use_id round-tripping happens
                # only when the higher-level loop uses the Claude tool_use path.)
                anthropic_messages.append({"role": "user", "content": content})
                continue
            anthropic_messages.append({"role": role, "content": content})

        return "\n\n".join(system_chunks), anthropic_messages

    # ------------------------------------------------------------------
    # unified_call — same signature as LiteLLMChatWrapper.unified_call
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

        system_text, anthropic_messages = self._convert_messages(msgs)

        call_kwargs: dict[str, Any] = {**self.kwargs, **kwargs}
        # Drop A0-only retry params that the Anthropic SDK does not accept.
        call_kwargs.pop("a0_retry_attempts", None)
        call_kwargs.pop("a0_retry_delay_seconds", None)

        max_tokens = int(call_kwargs.pop("max_tokens", 4096))

        # Extended thinking — enabled per-call when budget > 0.
        thinking_budget = int(call_kwargs.pop("thinking_budget", self.thinking_budget))
        if thinking_budget > 0:
            call_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }

        stream = (
            reasoning_callback is not None
            or response_callback is not None
            or tokens_callback is not None
        )

        request_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": max_tokens,
            "messages": anthropic_messages,
        }
        if system_text:
            request_kwargs["system"] = system_text
        request_kwargs.update(call_kwargs)

        response_text = ""
        reasoning_text = ""

        if stream:
            # AsyncAnthropic.messages.stream() yields typed events with a
            # final accumulated message available via ``get_final_message()``.
            async with self._client.messages.stream(**request_kwargs) as event_stream:
                async for event in event_stream:
                    delta_text, delta_thinking = self._event_deltas(event)
                    if delta_thinking:
                        reasoning_text += delta_thinking
                        if reasoning_callback:
                            await reasoning_callback(delta_thinking, reasoning_text)
                        if tokens_callback:
                            await tokens_callback(delta_thinking, _approx_tokens(delta_thinking))
                    if delta_text:
                        response_text += delta_text
                        if response_callback:
                            await response_callback(delta_text, response_text)
                        if tokens_callback:
                            await tokens_callback(delta_text, _approx_tokens(delta_text))
        else:
            msg = await self._client.messages.create(**request_kwargs)
            extracted = extract_response_blocks(getattr(msg, "content", []))
            response_text = extracted.text
            reasoning_text = extracted.thinking

        return response_text, reasoning_text

    # ------------------------------------------------------------------
    # Optional streaming generator (parity with LiteLLMChatWrapper._astream)
    # ------------------------------------------------------------------

    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        system_text, anthropic_messages = self._convert_messages(messages)
        request_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": int(kwargs.pop("max_tokens", 4096)),
            "messages": anthropic_messages,
        }
        if system_text:
            request_kwargs["system"] = system_text
        request_kwargs.update(kwargs)

        async with self._client.messages.stream(**request_kwargs) as event_stream:
            async for event in event_stream:
                delta_text, _ = self._event_deltas(event)
                if delta_text:
                    yield delta_text

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _event_deltas(event: Any) -> tuple[str, str]:
        """Extract (text_delta, thinking_delta) from an Anthropic stream event.

        Anthropic's SDK exposes typed events; we only care about
        ``content_block_delta`` events with ``text_delta`` or ``thinking_delta``
        sub-types. We probe attributes defensively to stay robust across SDK
        minor versions.
        """
        etype = getattr(event, "type", "") or (event.get("type", "") if isinstance(event, dict) else "")
        if etype != "content_block_delta":
            return "", ""
        delta = getattr(event, "delta", None) if not isinstance(event, dict) else event.get("delta")
        if delta is None:
            return "", ""
        dtype = getattr(delta, "type", "") or (delta.get("type", "") if isinstance(delta, dict) else "")
        if dtype == "text_delta":
            text = getattr(delta, "text", "") or (delta.get("text", "") if isinstance(delta, dict) else "")
            return str(text), ""
        if dtype == "thinking_delta":
            text = getattr(delta, "thinking", "") or (delta.get("thinking", "") if isinstance(delta, dict) else "")
            return "", str(text)
        return "", ""


def _approx_tokens(text: str) -> int:
    # Lightweight estimate; the real ``approximate_tokens`` lives in
    # python.helpers.tokens but importing it here would create a cycle on first
    # provider selection in some contexts.
    return max(1, len(text) // 4)
