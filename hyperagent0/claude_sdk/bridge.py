"""Bridge between Agent Zero Tool classes / monologue dispatch and the Claude Messages API.

Three responsibilities (spec 02 task 1.2):

1. ``tool_to_claude_schema`` — translate an Agent Zero ``Tool`` subclass into the
   Claude ``tools=[...]`` JSON schema by introspecting ``execute(**kwargs)``.

2. ``map_tool_use_to_dispatch`` — convert a Claude ``tool_use`` content block
   into the JSON envelope Agent Zero's ``process_tools`` already understands
   ({"tool_name": ..., "tool_args": {...}}).

3. ``extract_response_blocks`` — pull (thinking, text, tool_uses) out of a
   Claude ``Message.content`` list (the response from the Messages API).

No ``anthropic`` import here — these helpers operate on plain dicts so they are
safe to call (and unit-test) without the ``[claude-sdk]`` extra installed.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Tool class -> Claude tool schema
# ---------------------------------------------------------------------------


_DOCSTRING_SPLIT = re.compile(r"\n\s*\n")


def _docstring_summary(cls: type) -> str:
    doc = inspect.getdoc(cls) or ""
    if not doc:
        return ""
    # Use the first paragraph as the Claude tool description.
    return _DOCSTRING_SPLIT.split(doc, 1)[0].strip()


_STRING_TYPE_MAP: dict[str, dict[str, Any]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "list": {"type": "array", "items": {"type": "string"}},
    "dict": {"type": "object"},
}


def _python_type_to_json(annotation: Any) -> dict[str, Any]:
    """Best-effort Python annotation -> JSON Schema fragment.

    Handles both actual types and string annotations (PEP 563 /
    ``from __future__ import annotations``). Falls back to
    ``{"type": "string"}`` for anything unrecognised — Agent Zero tools accept
    loose JSON dicts so this is a safe default.
    """
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    if isinstance(annotation, str):
        # Strip Optional[X] / X | None when expressed as a string.
        stripped = annotation.strip()
        if stripped.startswith("Optional[") and stripped.endswith("]"):
            stripped = stripped[len("Optional["):-1]
        if " | " in stripped:
            parts = [p.strip() for p in stripped.split("|") if p.strip() and p.strip() != "None"]
            if len(parts) == 1:
                stripped = parts[0]
        return _STRING_TYPE_MAP.get(stripped, {"type": "string"})
    # Strip Optional[X] / X | None on real types.
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    if origin is not None and args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _python_type_to_json(non_none[0])
    mapping = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        list: {"type": "array", "items": {"type": "string"}},
        dict: {"type": "object"},
    }
    return mapping.get(annotation, {"type": "string"})


def tool_to_claude_schema(tool_cls: type, name: str | None = None) -> dict[str, Any]:
    """Convert an Agent Zero ``Tool`` subclass into a Claude tool schema.

    The Agent Zero ``Tool.execute`` signature is ``async def execute(self, **kwargs)``
    in most tools — kwargs are populated from the JSON ``tool_args`` dict, so we
    surface a permissive object schema. Concrete tools that pin keyword names get
    a tighter schema by inspecting their ``execute`` parameters.
    """
    execute = getattr(tool_cls, "execute", None)
    properties: dict[str, Any] = {}
    required: list[str] = []
    additional_properties = True

    if execute is not None:
        try:
            sig = inspect.signature(execute)
        except (TypeError, ValueError):
            sig = None
        if sig is not None:
            for pname, param in sig.parameters.items():
                if pname in ("self", "kwargs"):
                    continue
                if param.kind in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                ):
                    continue
                properties[pname] = _python_type_to_json(param.annotation)
                if param.default is inspect.Parameter.empty:
                    required.append(pname)

    if not properties:
        # Fully dynamic kwargs — keep the schema permissive so Claude does not
        # have to invent fake parameters.
        return {
            "name": name or tool_cls.__name__,
            "description": _docstring_summary(tool_cls) or tool_cls.__name__,
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
        }

    schema: dict[str, Any] = {
        "name": name or tool_cls.__name__,
        "description": _docstring_summary(tool_cls) or tool_cls.__name__,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "additionalProperties": additional_properties,
        },
    }
    if required:
        schema["input_schema"]["required"] = required
    return schema


# ---------------------------------------------------------------------------
# Claude tool_use blocks -> Agent Zero dispatch envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolUse:
    """Lightweight tool_use representation extracted from a Claude response."""

    id: str
    name: str
    input: dict[str, Any]


def map_tool_use_to_dispatch(tool_use: ToolUse | dict[str, Any]) -> str:
    """Convert a Claude tool_use block into Agent Zero's JSON envelope.

    Agent Zero's ``Agent.process_tools`` parses the assistant message with
    ``extract_tools.json_parse_dirty`` and expects:

        {"tool_name": "...", "tool_args": {...}}

    Returning a JSON string keeps the dispatch path identical to the LiteLLM
    text-mode flow.
    """
    if isinstance(tool_use, ToolUse):
        name = tool_use.name
        args = tool_use.input
    else:
        name = tool_use.get("name", "")
        args = tool_use.get("input", {}) or {}
    return json.dumps({"tool_name": name, "tool_args": args})


# ---------------------------------------------------------------------------
# Response content -> (thinking, text, tool_uses)
# ---------------------------------------------------------------------------


@dataclass
class ExtractedResponse:
    thinking: str
    text: str
    tool_uses: list[ToolUse]

    def as_tuple(self) -> tuple[str, str, list[ToolUse]]:
        return self.thinking, self.text, self.tool_uses


def _block_type(block: Any) -> str:
    if isinstance(block, dict):
        return block.get("type", "")
    return getattr(block, "type", "")


def _block_get(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def extract_response_blocks(content: Iterable[Any] | None) -> ExtractedResponse:
    """Split a Claude Messages response ``content`` array into its parts.

    Accepts either SDK objects (``anthropic.types.TextBlock`` etc.) or plain dicts
    so the function is dependency-free and unit-testable with synthetic payloads.
    """
    thinking_parts: list[str] = []
    text_parts: list[str] = []
    tool_uses: list[ToolUse] = []

    for block in content or []:
        btype = _block_type(block)
        if btype == "thinking":
            thought = _block_get(block, "thinking") or _block_get(block, "text") or ""
            if thought:
                thinking_parts.append(str(thought))
        elif btype == "redacted_thinking":
            # Anthropic returns opaque redacted_thinking blocks when the policy
            # filter trips; we surface a marker so callers can pass it back on
            # follow-up turns (required for thinking continuity).
            thinking_parts.append("[redacted_thinking]")
        elif btype == "text":
            text_parts.append(str(_block_get(block, "text") or ""))
        elif btype == "tool_use":
            tool_uses.append(
                ToolUse(
                    id=str(_block_get(block, "id", "") or ""),
                    name=str(_block_get(block, "name", "") or ""),
                    input=dict(_block_get(block, "input", {}) or {}),
                )
            )

    return ExtractedResponse(
        thinking="\n".join(thinking_parts).strip(),
        text="".join(text_parts),
        tool_uses=tool_uses,
    )
