"""Tests for the eval harness scoring (no network)."""

from __future__ import annotations

import json

from evals.tool_choice_eval import percentile, score

TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}
OTHER = {
    "type": "function",
    "function": {"name": "search_docs", "parameters": {"type": "object", "properties": {}}},
}
SCENARIO = {"id": "s", "tools": [TOOL, OTHER], "expect_tool": "get_weather"}


def _completion(message: dict) -> bytes:
    return json.dumps({"choices": [{"index": 0, "message": message}]}).encode()


def _call(name: str, arguments: str) -> dict:
    return {"id": "c", "type": "function", "function": {"name": name, "arguments": arguments}}


def test_valid_tool_call_scores_ok() -> None:
    body = _completion({"tool_calls": [_call("get_weather", '{"city": "Lisbon"}')]})

    assert score(SCENARIO, 200, body, stream=False) == (True, "ok")


def test_streamed_tool_call_with_split_arguments_scores_ok() -> None:
    first = {"index": 0, "id": "c", "type": "function", "function": {"name": "get_weather"}}
    events = [
        {"choices": [{"index": 0, "delta": {"tool_calls": [first]}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city": '}}]},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"Lisbon"}'}}]},
                }
            ]
        },
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"

    assert score(SCENARIO, 200, body, stream=True) == (True, "ok")


def test_wrong_tool_fails() -> None:
    body = _completion({"tool_calls": [_call("search_docs", "{}")]})

    assert score(SCENARIO, 200, body, stream=False) == (False, "wrong_tool:search_docs")


def test_invalid_arguments_fail() -> None:
    body = _completion({"tool_calls": [_call("get_weather", '{"city": 42}')]})

    assert score(SCENARIO, 200, body, stream=False) == (False, "invalid_arguments")


def test_text_answer_fails() -> None:
    body = _completion({"content": "It is sunny in Lisbon."})

    assert score(SCENARIO, 200, body, stream=False) == (False, "no_tool_call")


def test_http_error_fails_with_status() -> None:
    assert score(SCENARIO, 400, b'{"error": {}}', stream=False) == (False, "http_400")


def test_any_declared_tool_is_accepted_when_none_is_expected() -> None:
    body = _completion({"tool_calls": [_call("search_docs", "{}")]})

    assert score({**SCENARIO, "expect_tool": None}, 200, body, stream=False) == (True, "ok")


def test_percentile_uses_nearest_rank() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 10.0]

    assert percentile(values, 50) == 3.0
    assert percentile(values, 95) == 10.0
    assert percentile([], 50) is None


def test_summary_counts_stalled_turns_separately() -> None:
    from evals.tool_choice_eval import summarize

    records = [
        {"target": "t", "ok": True, "reason": "ok", "outcome": "native", "seconds": 1.0},
        {"target": "t", "ok": False, "reason": "no_tool_call", "outcome": "failed", "seconds": 1.0},
        {
            "target": "t",
            "ok": False,
            "reason": "wrong_tool:read",
            "outcome": "rescued",
            "seconds": 1.0,
        },
        {"target": "t", "ok": False, "reason": "http_500", "outcome": "rewritten", "seconds": 1.0},
    ]

    row = summarize(records).splitlines()[2]

    assert "| 1/4 (25%, 95% CI" in row
    assert "| 1/3 (33%, 95% CI" in row, "stalled turns exclude upstream HTTP errors"


def test_progress_accepts_any_offered_tool_with_valid_arguments() -> None:
    from evals.tool_choice_eval import progress

    other_tool = _completion({"tool_calls": [_call("search_docs", "{}")]})
    bad_args = _completion({"tool_calls": [_call("get_weather", '{"city": 42}')]})
    text = _completion({"content": "sunny"})

    assert progress(SCENARIO, 200, other_tool, stream=False) is True
    assert progress(SCENARIO, 200, bad_args, stream=False) is False
    assert progress(SCENARIO, 200, text, stream=False) is False
    assert progress(SCENARIO, 500, other_tool, stream=False) is False


def test_summary_reports_progress_with_confidence_intervals() -> None:
    from evals.tool_choice_eval import summarize

    records = [
        {
            "target": "t",
            "ok": True,
            "progress": True,
            "reason": "ok",
            "outcome": "native",
            "seconds": 1.0,
        },
        {
            "target": "t",
            "ok": False,
            "progress": True,
            "reason": "wrong_tool:read",
            "outcome": "rescued",
            "seconds": 1.0,
        },
    ]

    header, _, row = summarize(records).splitlines()

    assert "Progress" in header
    assert "| 1/2 (50%, 95% CI" in row
    assert "| 2/2 (100%, 95% CI" in row
