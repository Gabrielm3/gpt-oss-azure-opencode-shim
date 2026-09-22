"""Forced-tool-call polyfill.

Azure-hosted GPT-OSS rejects a forced ``tool_choice``, so the shim rewrites it
to ``"auto"``. With ``"auto"`` the model often does the work but delivers the
final answer as JSON text instead of calling the tool the client required (see
``docs/PROBLEM.md``). This module inspects the upstream response of a request
that was forced and, when the text is a JSON object that matches the schema of
exactly one candidate tool, returns it as that tool call.

Everything here is pure: bytes in, bytes and an :class:`Outcome` out.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from referencing.exceptions import Unresolvable


class Outcome(str, Enum):
    """What the shim did with one request. Used for headers and metrics."""

    PASSTHROUGH = "passthrough"  # nothing forced, nothing changed
    REWRITTEN = "rewritten"  # forced tool_choice rewritten, response not inspected
    NATIVE = "native"  # forced; the model called a tool by itself
    RESCUED = "rescued"  # forced; JSON text converted into the tool call
    FAILED = "failed"  # forced; no tool call and nothing to convert
    EMPTY_CHOICES = "empty_choices"  # upstream answered HTTP 200 with no choice
    UPSTREAM_ERROR = "upstream_error"  # transport failure or timeout


@dataclass(frozen=True)
class ForcedToolChoice:
    """The function tools a forced request allows the model to call."""

    tools: tuple[dict[str, Any], ...]


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
# Upper bound on '{' positions tried when looking for JSON at the end of text.
_MAX_BRACE_SCAN = 64
_EMPTY_CHOICES_ERROR = {
    "error": {"message": "upstream returned no choices", "type": Outcome.EMPTY_CHOICES.value}
}


def forced_tool_choice(payload: Mapping[str, Any]) -> ForcedToolChoice | None:
    """Return the candidate tools if ``payload`` forces a tool call, else ``None``."""
    tools = [
        tool
        for tool in payload.get("tools") or []
        if isinstance(tool, dict)
        and tool.get("type") == "function"
        and isinstance(tool.get("function"), dict)
    ]
    if not tools:
        return None

    choice = payload.get("tool_choice")
    if choice == "required":
        return ForcedToolChoice(tuple(tools))
    if isinstance(choice, dict) and choice.get("type") == "function":
        name = (choice.get("function") or {}).get("name")
        named = [tool for tool in tools if tool["function"].get("name") == name]
        return ForcedToolChoice(tuple(named)) if named else None
    return None


def parse_json_text(text: str) -> Any | None:
    """Parse model text as JSON, allowing a Markdown code fence around it."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def match_tool(value: Any, tools: Iterable[Mapping[str, Any]]) -> str | None:
    """Return the name of the one tool whose parameters accept ``value``.

    Matching is strict: ``value`` must validate against the tool's JSON Schema
    and use only declared top-level properties. No match, or more than one,
    returns ``None``; the polyfill never guesses.
    """
    if not isinstance(value, dict):
        return None
    matches = [
        tool["function"]["name"]
        for tool in tools
        if _accepts(tool["function"].get("parameters"), value)
    ]
    return matches[0] if len(matches) == 1 else None


def _accepts(schema: Any, value: dict[str, Any]) -> bool:
    if not isinstance(schema, dict):
        return False
    if not set(value) <= set(schema.get("properties") or {}):
        return False
    try:
        validator_cls = validator_for(schema)
        validator_cls.check_schema(schema)
        return validator_cls(schema).is_valid(value)
    except (SchemaError, Unresolvable):
        return False


def trailing_json_object(text: str) -> dict[str, Any] | None:
    """Return the JSON object that ends ``text``, or ``None``.

    gpt-oss sometimes writes a tool call's arguments at the end of its
    reasoning instead of emitting the call. Only an object that closes the
    text counts; JSON in the middle of reasoning is ignored.
    """
    stripped = text.rstrip()
    if not stripped.endswith("}"):
        return None
    end = len(stripped)
    for _ in range(_MAX_BRACE_SCAN):
        start = stripped.rfind("{", 0, end)
        if start < 0:
            return None
        try:
            value = json.loads(stripped[start:])
        except ValueError:
            end = start
            continue
        return value if isinstance(value, dict) else None
    return None


def answer_as_tool_call(
    content: str, reasoning: str, tools: Iterable[Mapping[str, Any]]
) -> tuple[str, Any] | None:
    """Find a tool call hidden in a text answer: ``(tool name, arguments)``.

    First the answer text itself (JSON, maybe fenced). If the model wrote no
    text at all, then a JSON object at the end of its reasoning.
    """
    tools = tuple(tools)
    value = parse_json_text(content)
    name = match_tool(value, tools)
    if name is None and not content.strip():
        value = trailing_json_object(reasoning)
        name = match_tool(value, tools)
    return (name, value) if name is not None else None


def _reasoning(message: Mapping[str, Any]) -> str:
    """Reasoning text of a message or delta; providers use either field name."""
    return message.get("reasoning_content") or message.get("reasoning") or ""


def _tool_call(name: str, arguments: Any) -> dict[str, Any]:
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def repair_completion(body: bytes, forced: ForcedToolChoice) -> tuple[bytes, Outcome]:
    """Repair a non-streamed Chat Completions response to a forced request."""
    try:
        payload = json.loads(body)
    except ValueError:
        return body, Outcome.FAILED
    if not isinstance(payload, dict):
        return body, Outcome.FAILED

    choices = payload.get("choices")
    if choices == []:
        return body, Outcome.EMPTY_CHOICES
    if not isinstance(choices, list) or not isinstance(choices[0], dict):
        return body, Outcome.FAILED

    message = choices[0].get("message") or {}
    if message.get("tool_calls"):
        return body, Outcome.NATIVE

    found = answer_as_tool_call(message.get("content") or "", _reasoning(message), forced.tools)
    if found is None:
        return body, Outcome.FAILED
    name, value = found

    message["tool_calls"] = [_tool_call(name, value)]
    message["content"] = None
    choices[0]["message"] = message
    choices[0]["finish_reason"] = "tool_calls"
    return json.dumps(payload, ensure_ascii=False).encode(), Outcome.RESCUED


def repair_stream(body: bytes, forced: ForcedToolChoice) -> tuple[bytes, Outcome]:
    """Repair a buffered SSE Chat Completions stream to a forced request."""
    parsed = _parse_sse(body)
    if parsed is None or any("error" in event for event in parsed):
        return body, Outcome.FAILED

    choices = [choice for event in parsed for choice in event.get("choices") or []]
    if not choices:
        return _render([*parsed, _EMPTY_CHOICES_ERROR], done=False), Outcome.EMPTY_CHOICES
    if any((choice.get("delta") or {}).get("tool_calls") for choice in choices):
        return body, Outcome.NATIVE

    deltas = [choice.get("delta") or {} for choice in choices]
    found = answer_as_tool_call(
        "".join(d.get("content") or "" for d in deltas),
        "".join(_reasoning(d) for d in deltas),
        forced.tools,
    )
    if found is None:
        return body, Outcome.FAILED
    name, value = found

    kept, usage_only = [], []
    for event in parsed:
        remaining = []
        for choice in event.get("choices") or []:
            delta = {k: v for k, v in (choice.get("delta") or {}).items() if k != "content"}
            if delta:
                remaining.append({**choice, "delta": delta, "finish_reason": None})
        if remaining:
            kept.append({**event, "choices": remaining})
        elif event.get("usage"):
            usage_only.append({**event, "choices": []})

    head = {k: parsed[0][k] for k in ("id", "created", "model") if k in parsed[0]}
    head["object"] = "chat.completion.chunk"
    call = {"index": 0, **_tool_call(name, value)}
    tool_event = {
        **head,
        "choices": [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}],
    }
    finish_event = {
        **head,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    return _render([*kept, tool_event, finish_event, *usage_only], done=True), Outcome.RESCUED


def _parse_sse(body: bytes) -> list[dict[str, Any]] | None:
    """Return the JSON ``data:`` events of an SSE body, or ``None`` if malformed."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    events = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        events.append(event)
    return events


def _render(events: list[dict[str, Any]], *, done: bool) -> bytes:
    lines = [f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()
