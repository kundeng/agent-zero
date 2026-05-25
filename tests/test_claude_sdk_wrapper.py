"""Tests for hyperagent0.claude_sdk.wrapper (spec 02 D1 reframed: CLI auth path).

The wrapper now uses ``claude-agent-sdk`` (subprocess to the local
``claude`` CLI) instead of ``anthropic.AsyncAnthropic`` (API-key). These
tests inject a stub ``claude_agent_sdk`` module into ``sys.modules`` so
the wrapper can be exercised without the real SDK installed and without
needing a working ``claude`` CLI on PATH.

Aim: pin the *contract* the wrapper relies on (Options shape, message
types it inspects), not the SDK's own behavior.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, List

import pytest


# ---------------------------------------------------------------------------
# Stub claude_agent_sdk module
# ---------------------------------------------------------------------------


@dataclass
class _StubTextBlock:
    text: str


@dataclass
class _StubThinkingBlock:
    thinking: str
    signature: str = ""


@dataclass
class _StubToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class _StubAssistantMessage:
    content: list


@dataclass
class _StubResultMessage:
    subtype: str = "success"
    result: str = ""


@dataclass
class _StubClaudeAgentOptions:
    model: str | None = None
    max_turns: int | None = None
    allowed_tools: list = field(default_factory=list)
    system_prompt: str | None = None
    permission_mode: str | None = None
    cli_path: str | None = None
    max_thinking_tokens: int | None = None
    thinking: Any = None
    mcp_servers: dict = field(default_factory=dict)


@dataclass
class _StubThinkingConfigEnabled:
    type: str
    budget_tokens: int


class _Recorder:
    """Captures the prompt + options seen by the stub ``query`` so tests
    can assert how the wrapper invoked the SDK."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.scripted_messages: list[Any] = []

    def script(self, messages: list[Any]) -> None:
        self.scripted_messages = messages


def _install_stub_sdk(monkeypatch, recorder: _Recorder) -> types.ModuleType:
    mod = types.ModuleType("claude_agent_sdk")

    mod.TextBlock = _StubTextBlock
    mod.ThinkingBlock = _StubThinkingBlock
    mod.ToolUseBlock = _StubToolUseBlock
    mod.AssistantMessage = _StubAssistantMessage
    mod.ResultMessage = _StubResultMessage
    mod.ClaudeAgentOptions = _StubClaudeAgentOptions
    mod.ThinkingConfigEnabled = _StubThinkingConfigEnabled

    async def _fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        recorder.calls.append({"prompt": prompt, "options": options})
        for m in recorder.scripted_messages:
            yield m

    mod.query = _fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_wrapper_imports_lazy_and_uses_claude_agent_sdk(monkeypatch):
    """Constructing the wrapper should pull in claude_agent_sdk, not anthropic."""

    rec = _Recorder()
    _install_stub_sdk(monkeypatch, rec)

    # Poison anthropic so any accidental import explodes loudly.
    poison = types.ModuleType("anthropic")

    def _boom(*a, **kw):  # pragma: no cover - guard
        raise AssertionError("anthropic must NOT be imported by the wrapper")

    poison.AsyncAnthropic = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", poison)

    from hyperagent0.claude_sdk.wrapper import ClaudeSDKWrapper

    w = ClaudeSDKWrapper(model="claude-sonnet-4-5", api_key="should-be-discarded")
    assert w.provider == "claude-sdk"
    assert w.model_name == "claude-sonnet-4-5"
    # api_key is silently dropped — auth flows through the CLI.
    assert "api_key" not in w.kwargs


@pytest.mark.asyncio
async def test_unified_call_aggregates_text_and_thinking(monkeypatch):
    rec = _Recorder()
    sdk = _install_stub_sdk(monkeypatch, rec)
    rec.script(
        [
            sdk.AssistantMessage(
                content=[
                    sdk.ThinkingBlock(thinking="step 1"),
                    sdk.TextBlock(text="Hello, "),
                    sdk.TextBlock(text="world."),
                ]
            ),
            sdk.ResultMessage(subtype="success", result="Hello, world."),
        ]
    )

    from hyperagent0.claude_sdk.wrapper import ClaudeSDKWrapper

    w = ClaudeSDKWrapper(model="claude-sonnet-4-5")

    response_chunks: list[str] = []
    reasoning_chunks: list[str] = []

    async def on_resp(delta: str, total: str) -> None:
        response_chunks.append(delta)

    async def on_reason(delta: str, total: str) -> None:
        reasoning_chunks.append(delta)

    response, reasoning = await w.unified_call(
        system_message="be terse",
        user_message="hi",
        response_callback=on_resp,
        reasoning_callback=on_reason,
    )

    assert response == "Hello, world."
    assert reasoning == "step 1"
    assert response_chunks == ["Hello, ", "world."]
    assert reasoning_chunks == ["step 1"]

    # The wrapper must call query() exactly once with the user text as
    # prompt and the system text as options.system_prompt.
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["prompt"] == "hi"
    assert call["options"].system_prompt == "be terse"
    # allowed_tools=[] is the guardrail that prevents the SDK from invoking
    # its own Tool path — Agent Zero's monologue loop owns dispatch.
    assert call["options"].allowed_tools == []
    assert call["options"].max_turns == 1


@pytest.mark.asyncio
async def test_unified_call_ignores_non_assistant_messages(monkeypatch):
    """Result / Rate-limit / hook events must not pollute response text."""

    rec = _Recorder()
    sdk = _install_stub_sdk(monkeypatch, rec)

    # Add a fake non-assistant message class the wrapper hasn't heard of.
    class _Mystery:
        content = [sdk.TextBlock(text="should be ignored")]

    rec.script(
        [
            _Mystery(),
            sdk.AssistantMessage(content=[sdk.TextBlock(text="real answer")]),
            sdk.ResultMessage(),
        ]
    )

    from hyperagent0.claude_sdk.wrapper import ClaudeSDKWrapper

    w = ClaudeSDKWrapper(model="claude-sonnet-4-5")
    response, reasoning = await w.unified_call(user_message="ping")
    assert response == "real answer"
    assert reasoning == ""


@pytest.mark.asyncio
async def test_cli_path_and_thinking_budget_propagate(monkeypatch):
    rec = _Recorder()
    sdk = _install_stub_sdk(monkeypatch, rec)
    rec.script([sdk.AssistantMessage(content=[sdk.TextBlock(text="ok")])])

    from hyperagent0.claude_sdk.wrapper import ClaudeSDKWrapper

    w = ClaudeSDKWrapper(
        model="claude-sonnet-4-5",
        cli_path="/opt/homebrew/bin/claude",
        thinking_budget=8000,
        max_turns=2,
    )
    await w.unified_call(user_message="hi")

    opts = rec.calls[0]["options"]
    assert opts.cli_path == "/opt/homebrew/bin/claude"
    assert opts.max_thinking_tokens == 8000
    assert opts.max_turns == 2
    # ThinkingConfigEnabled object lands in opts.thinking.
    assert opts.thinking is not None
    assert opts.thinking.budget_tokens == 8000


@pytest.mark.asyncio
async def test_astream_yields_only_text_deltas(monkeypatch):
    rec = _Recorder()
    sdk = _install_stub_sdk(monkeypatch, rec)
    rec.script(
        [
            sdk.AssistantMessage(
                content=[
                    sdk.ThinkingBlock(thinking="hidden"),
                    sdk.TextBlock(text="alpha"),
                    sdk.TextBlock(text="beta"),
                ]
            ),
        ]
    )

    from hyperagent0.claude_sdk.wrapper import ClaudeSDKWrapper
    from langchain_core.messages import HumanMessage

    w = ClaudeSDKWrapper(model="claude-sonnet-4-5")
    chunks: list[str] = []
    async for c in w._astream([HumanMessage(content="hi")]):
        chunks.append(c)
    assert chunks == ["alpha", "beta"]


def test_wrapper_raises_clear_error_when_sdk_missing(monkeypatch):
    """If the [claude-sdk] extra isn't installed, fail with an install hint."""

    # Force the import to fail by removing any cached + stub modules.
    monkeypatch.delitem(sys.modules, "claude_agent_sdk", raising=False)

    # Make the import statement itself raise.
    original_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__  # type: ignore[index]

    def _no_sdk(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "claude_agent_sdk":
            raise ImportError("no claude_agent_sdk")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", _no_sdk)

    from hyperagent0.claude_sdk.wrapper import ClaudeSDKWrapper

    with pytest.raises(ImportError) as ei:
        ClaudeSDKWrapper(model="claude-sonnet-4-5")
    assert "claude-sdk" in str(ei.value).lower()
