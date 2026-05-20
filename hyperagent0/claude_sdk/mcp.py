"""Claude SDK <-> Agent Zero MCP bridge (spec 02 task 1.6).

Agent Zero's ``python.helpers.mcp_handler`` discovers MCP tools and surfaces
them as Agent Zero ``Tool`` instances. The Claude Messages API natively accepts
``tools=[...]`` definitions and emits ``tool_use`` blocks.

This module translates between the two representations so that, when the active
provider is ``claude-sdk``, MCP tools registered in Agent Zero are *also*
visible to Claude in its native schema.

The integration point is ``register_mcp_tools_for_claude`` — called by the
minimal ``mcp_handler.py`` patch when ``chat_model_provider == "claude-sdk"``.

No ``anthropic`` import here — we produce plain dict schemas that match what
``anthropic.AsyncAnthropic().messages.create(tools=[...])`` accepts.
"""

from __future__ import annotations

from typing import Any


def mcp_tool_to_claude_schema(server_name: str, tool: dict[str, Any]) -> dict[str, Any]:
    """Convert one MCP tool descriptor into a Claude tool definition.

    Agent Zero stores MCP tools as dicts with ``name``, ``description`` and
    ``input_schema`` (already JSON Schema). The Claude tool format is::

        {"name": str, "description": str, "input_schema": {...}}

    We prefix the tool name with the MCP server to match Agent Zero's existing
    dispatch convention (``{server}.{tool}``).
    """
    raw_name = tool.get("name", "")
    qualified = f"{server_name}.{raw_name}" if server_name else raw_name
    # Claude tool names must match ^[a-zA-Z0-9_-]{1,64}$; periods are not
    # permitted, so collapse "{server}.{tool}" with an underscore for the wire
    # format while keeping the original mapping for dispatch.
    wire_name = qualified.replace(".", "_")[:64]
    return {
        "name": wire_name,
        "description": tool.get("description", "") or raw_name,
        "input_schema": tool.get("input_schema") or {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
        # Non-API metadata for dispatch resolution on the Agent Zero side.
        "_a0_dispatch_name": qualified,
    }


def register_mcp_tools_for_claude(mcp_tools: list[dict[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Translate the full MCPConfig tool listing into Claude-native schemas.

    ``mcp_tools`` is the structure returned by ``MCPConfig.get_tools()`` — a
    list of single-key dicts mapping ``"{server}.{tool}" -> tool_descriptor``.
    """
    out: list[dict[str, Any]] = []
    for entry in mcp_tools or []:
        # Each entry is a {qualified_name: descriptor} dict.
        for _qualified, descriptor in entry.items():
            server_name = descriptor.get("server", "")
            out.append(mcp_tool_to_claude_schema(server_name, descriptor))
    return out


def is_claude_sdk_active() -> bool:
    """Return True when the current chat-model provider is ``claude-sdk``.

    Safe to call from ``mcp_handler.py`` — the import is local and tolerates
    settings being unavailable (e.g. during early bootstrap).
    """
    try:
        from python.helpers import settings  # local import to avoid cycles
        current = settings.get_settings()
    except Exception:
        return False
    return str(current.get("chat_model_provider", "")).lower() == "claude-sdk"
