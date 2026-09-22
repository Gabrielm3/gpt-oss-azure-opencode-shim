"""Tests for the forced-tool-call polyfill (pure functions, no HTTP)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gpt_oss_shim.polyfill import (
    Outcome,
    forced_tool_choice,
    match_tool,
    parse_json_text,
    repair_completion,
    repair_stream,
)

STRUCTURED = {
    "type": "function",
    "function": {
        "name": "StructuredOutput",
        "parameters": {
            "type": "object",
            "properties": {"files": {"type": "array", "items": {"type": "string"}}},
            "required": ["files"],
        },
    },
}
GLOB = {
    "type": "function",
    "function": {
        "name": "glob",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern"],
        },
    },
}
# A tool that accepts almost anything; strict matching must not pick it by accident.
PERMISSIVE = {
    "type": "function",
    "function": {"name": "note", "parameters": {"type": "object", "properties": {}}},
}


def _sse(*events: dict[str, Any] | str) -> bytes:
    lines = [f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n" for e in events]
    return "".join(lines).encode()


def _events(body: bytes) -> list[Any]:
    out = []
    for line in body.decode().splitlines():
        if line.startswith("data: "):
            data = line[len("data: ") :]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


def _chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-oss-120b",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **extra,
    }


# --- forced_tool_choice -----------------------------------------------------


def test_required_makes_every_function_tool_a_candidate() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [GLOB, STRUCTURED]})

    assert forced is not None
    assert [t["function"]["name"] for t in forced.tools] == ["glob", "StructuredOutput"]


def test_forced_function_restricts_candidates_to_that_tool() -> None:
    payload = {
        "tool_choice": {"type": "function", "function": {"name": "glob"}},
        "tools": [GLOB, STRUCTURED],
    }

    forced = forced_tool_choice(payload)

    assert forced is not None
    assert [t["function"]["name"] for t in forced.tools] == ["glob"]


@pytest.mark.parametrize("choice", ["auto", "none", None])
def test_unforced_choices_are_not_polyfilled(choice: str | None) -> None:
    assert forced_tool_choice({"tool_choice": choice, "tools": [GLOB]}) is None


def test_forced_choice_without_tools_is_not_polyfilled() -> None:
    assert forced_tool_choice({"tool_choice": "required"}) is None


# --- parse_json_text / match_tool -------------------------------------------


@pytest.mark.parametrize(
    "text",
    ['{"files": ["a.md"]}', '  {"files": ["a.md"]}\n', '```json\n{"files": ["a.md"]}\n```'],
)
def test_parse_json_text_accepts_plain_and_fenced_json(text: str) -> None:
    assert parse_json_text(text) == {"files": ["a.md"]}


@pytest.mark.parametrize("text", ["", "The files are a.md and b.md.", "{not json"])
def test_parse_json_text_rejects_non_json(text: str) -> None:
    assert parse_json_text(text) is None


def test_match_tool_picks_the_single_schema_match() -> None:
    assert match_tool({"files": ["a.md"]}, (GLOB, STRUCTURED, PERMISSIVE)) == "StructuredOutput"


def test_match_tool_rejects_values_with_undeclared_keys() -> None:
    assert match_tool({"files": ["a.md"], "extra": 1}, (STRUCTURED,)) is None


def test_match_tool_rejects_schema_violations() -> None:
    assert match_tool({"files": "a.md"}, (STRUCTURED,)) is None


def test_match_tool_rejects_ambiguous_matches() -> None:
    twin = json.loads(json.dumps(STRUCTURED))
    twin["function"]["name"] = "StructuredOutputCopy"

    assert match_tool({"files": ["a.md"]}, (STRUCTURED, twin)) is None


def test_match_tool_rejects_non_objects() -> None:
    assert match_tool(["a.md"], (STRUCTURED,)) is None


# --- repair_completion (non-streaming) --------------------------------------


def _completion(message: dict[str, Any], finish: str = "stop") -> bytes:
    return json.dumps(
        {
            "id": "c1",
            "object": "chat.completion",
            "model": "gpt-oss-120b",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode()


def test_completion_with_json_text_is_rescued_as_tool_call() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [GLOB, STRUCTURED]})
    body = _completion({"role": "assistant", "content": '{"files": ["a.md", "b.md"]}'})

    repaired, outcome = repair_completion(body, forced)

    assert outcome is Outcome.RESCUED
    choice = json.loads(repaired)["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    call = choice["message"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["id"].startswith("call_")
    assert call["function"]["name"] == "StructuredOutput"
    assert json.loads(call["function"]["arguments"]) == {"files": ["a.md", "b.md"]}
    assert json.loads(repaired)["usage"]["total_tokens"] == 15


def test_completion_with_native_tool_call_is_unchanged() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [GLOB]})
    call = {"id": "x", "type": "function", "function": {"name": "glob", "arguments": "{}"}}
    body = _completion({"role": "assistant", "content": "", "tool_calls": [call]}, "tool_calls")

    repaired, outcome = repair_completion(body, forced)

    assert outcome is Outcome.NATIVE
    assert repaired == body


def test_completion_with_prose_is_left_as_failed() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [STRUCTURED]})
    body = _completion({"role": "assistant", "content": "The files are a.md and b.md."})

    repaired, outcome = repair_completion(body, forced)

    assert outcome is Outcome.FAILED
    assert repaired == body


def test_completion_with_empty_choices_is_reported() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [STRUCTURED]})
    body = json.dumps({"id": "c1", "choices": []}).encode()

    _, outcome = repair_completion(body, forced)

    assert outcome is Outcome.EMPTY_CHOICES


def test_completion_that_is_not_json_is_left_as_failed() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [STRUCTURED]})

    repaired, outcome = repair_completion(b"<html>", forced)

    assert outcome is Outcome.FAILED
    assert repaired == b"<html>"


# --- repair_stream (SSE) ----------------------------------------------------


def test_stream_with_json_text_is_rescued_as_tool_call() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [GLOB, STRUCTURED]})
    body = _sse(
        _chunk({"role": "assistant", "reasoning_content": "List them."}),
        _chunk({"content": '{"files": '}),
        _chunk({"content": '["a.md"]}'}, "stop"),
        {"id": "c1", "choices": [], "usage": {"prompt_tokens": 10, "total_tokens": 15}},
        "[DONE]",
    )

    repaired, outcome = repair_stream(body, forced)

    assert outcome is Outcome.RESCUED
    events = _events(repaired)
    assert events[-1] == "[DONE]"
    deltas = [c["delta"] for e in events[:-1] for c in e.get("choices", [])]
    assert not any(d.get("content") for d in deltas), (
        "the JSON text must not also stream as content"
    )
    assert any(d.get("reasoning_content") == "List them." for d in deltas), "reasoning is kept"
    calls = [tc for d in deltas for tc in d.get("tool_calls", [])]
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "StructuredOutput"
    assert json.loads(calls[0]["function"]["arguments"]) == {"files": ["a.md"]}
    finishes = [c["finish_reason"] for e in events[:-1] for c in e.get("choices", [])]
    assert [f for f in finishes if f] == ["tool_calls"]
    assert any(e.get("usage") for e in events[:-1]), "usage is kept"


def test_stream_with_native_tool_call_is_unchanged() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [GLOB]})
    call = {
        "index": 0,
        "id": "x",
        "type": "function",
        "function": {"name": "glob", "arguments": "{}"},
    }
    body = _sse(
        _chunk({"role": "assistant", "tool_calls": [call]}), _chunk({}, "tool_calls"), "[DONE]"
    )

    repaired, outcome = repair_stream(body, forced)

    assert outcome is Outcome.NATIVE
    assert repaired == body


def test_stream_with_prose_is_left_as_failed() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [STRUCTURED]})
    body = _sse(_chunk({"content": "The files are a.md."}, "stop"), "[DONE]")

    repaired, outcome = repair_stream(body, forced)

    assert outcome is Outcome.FAILED
    assert repaired == body


def test_stream_with_error_event_is_left_as_failed() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [STRUCTURED]})
    body = _sse({"error": {"message": "DFLASH speculative decoding ..."}}, "[DONE]")

    repaired, outcome = repair_stream(body, forced)

    assert outcome is Outcome.FAILED
    assert repaired == body


def test_stream_without_any_choice_is_reported_and_ends_with_error_event() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [STRUCTURED]})
    body = _sse({"id": "c1", "choices": [], "usage": {"total_tokens": 2}}, "[DONE]")

    repaired, outcome = repair_stream(body, forced)

    assert outcome is Outcome.EMPTY_CHOICES
    events = _events(repaired)
    assert "[DONE]" not in events
    assert events[-1]["error"]["type"] == "empty_choices"


# --- tool call leaked into the reasoning channel ----------------------------

READ = {
    "type": "function",
    "function": {
        "name": "read",
        "parameters": {
            "type": "object",
            "properties": {"filePath": {"type": "string"}},
            "required": ["filePath"],
        },
    },
}
LEAKED = 'Let\'s open the whole file.{"filePath": "pyproject.toml"}'


def test_completion_with_tool_call_leaked_into_reasoning_is_rescued() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [GLOB, READ, STRUCTURED]})
    body = _completion({"role": "assistant", "content": "", "reasoning_content": LEAKED})

    repaired, outcome = repair_completion(body, forced)

    assert outcome is Outcome.RESCUED
    message = json.loads(repaired)["choices"][0]["message"]
    call = message["tool_calls"][0]["function"]
    assert call["name"] == "read"
    assert json.loads(call["arguments"]) == {"filePath": "pyproject.toml"}
    assert message["reasoning_content"] == LEAKED


def test_reasoning_json_must_end_the_reasoning() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [READ]})
    reasoning = 'Maybe {"filePath": "pyproject.toml"} but first think more.'
    body = _completion({"role": "assistant", "content": "", "reasoning_content": reasoning})

    _, outcome = repair_completion(body, forced)

    assert outcome is Outcome.FAILED


def test_reasoning_is_ignored_when_the_model_wrote_prose() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [READ]})
    body = _completion({"role": "assistant", "content": "Here it is.", "reasoning_content": LEAKED})

    _, outcome = repair_completion(body, forced)

    assert outcome is Outcome.FAILED


def test_nested_json_at_the_end_of_reasoning_is_parsed_whole() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [GLOB, STRUCTURED]})
    reasoning = 'Done. {"files": ["a.md", "b.md"]}'
    body = _completion({"role": "assistant", "content": None, "reasoning_content": reasoning})

    repaired, outcome = repair_completion(body, forced)

    assert outcome is Outcome.RESCUED
    call = json.loads(repaired)["choices"][0]["message"]["tool_calls"][0]["function"]
    assert json.loads(call["arguments"]) == {"files": ["a.md", "b.md"]}


def test_stream_with_tool_call_leaked_into_reasoning_is_rescued() -> None:
    forced = forced_tool_choice({"tool_choice": "required", "tools": [GLOB, READ]})
    body = _sse(
        _chunk({"role": "assistant", "reasoning_content": "Let's open the whole file."}),
        _chunk({"reasoning_content": '{"filePath": "pyproject.toml"}'}),
        _chunk({"content": ""}, "stop"),
        "[DONE]",
    )

    repaired, outcome = repair_stream(body, forced)

    assert outcome is Outcome.RESCUED
    events = _events(repaired)
    calls = [
        tc
        for e in events[:-1]
        for c in e.get("choices", [])
        for tc in c["delta"].get("tool_calls", [])
    ]
    assert [c["function"]["name"] for c in calls] == ["read"]
