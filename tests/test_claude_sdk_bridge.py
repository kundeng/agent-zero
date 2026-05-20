"""Tests for hyperagent0.claude_sdk.bridge (spec 02-claude-sdk P2 task 2.3).

These tests intentionally use synthetic dict payloads so they run without the
``[claude-sdk]`` extra installed (no ``anthropic`` import path).
"""

from __future__ import annotations

import json

import pytest

from hyperagent0.claude_sdk.bridge import (
    ExtractedResponse,
    ToolUse,
    extract_response_blocks,
    map_tool_use_to_dispatch,
    tool_to_claude_schema,
)


class _PinnedTool:
    """A tool with one required and one default-valued kwarg."""

    async def execute(self, query: str, limit: int = 10, **kwargs):
        return None


class _DynamicTool:
    """Free-form kwargs only — no fixed parameters."""

    async def execute(self, **kwargs):
        return None


def test_tool_schema_pinned_kwargs():
    s = tool_to_claude_schema(_PinnedTool, name="pinned_tool")
    assert s["name"] == "pinned_tool"
    props = s["input_schema"]["properties"]
    assert props["query"] == {"type": "string"}
    assert props["limit"] == {"type": "integer"}
    assert s["input_schema"]["required"] == ["query"]
    assert s["input_schema"]["additionalProperties"] is True


def test_tool_schema_dynamic_kwargs():
    s = tool_to_claude_schema(_DynamicTool)
    assert s["input_schema"]["properties"] == {}
    assert s["input_schema"]["additionalProperties"] is True
    assert "required" not in s["input_schema"]


def test_extract_response_blocks_mixed_content():
    content = [
        {"type": "thinking", "thinking": "step one"},
        {"type": "text", "text": "Hello, "},
        {"type": "tool_use", "id": "tu_1", "name": "ping", "input": {"host": "x"}},
        {"type": "text", "text": "world."},
    ]
    extracted = extract_response_blocks(content)
    assert extracted.thinking == "step one"
    assert extracted.text == "Hello, world."
    assert len(extracted.tool_uses) == 1
    assert extracted.tool_uses[0] == ToolUse(id="tu_1", name="ping", input={"host": "x"})


def test_extract_response_blocks_empty():
    assert extract_response_blocks(None) == ExtractedResponse(
        thinking="", text="", tool_uses=[]
    )


def test_extract_response_blocks_redacted_thinking_marker():
    extracted = extract_response_blocks([{"type": "redacted_thinking"}])
    assert "[redacted_thinking]" in extracted.thinking


def test_map_tool_use_to_dispatch_envelope():
    envelope = json.loads(
        map_tool_use_to_dispatch(
            ToolUse(id="tu_2", name="code_execution_tool", input={"runtime": "python"})
        )
    )
    assert envelope == {
        "tool_name": "code_execution_tool",
        "tool_args": {"runtime": "python"},
    }


def test_map_tool_use_to_dispatch_accepts_plain_dict():
    envelope = json.loads(
        map_tool_use_to_dispatch({"name": "x", "input": {"a": 1}})
    )
    assert envelope == {"tool_name": "x", "tool_args": {"a": 1}}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
