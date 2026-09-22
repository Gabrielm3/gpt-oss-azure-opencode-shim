"""Tests for promoting traces into regression fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.fixtures import build_fixture, main, replay

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
PLUGIN_TOOL = {
    "type": "function",
    "function": {"name": "private_plugin", "parameters": {"type": "object", "properties": {}}},
}
RESPONSE = (
    'data: {"id": "c1", "choices": [{"index": 0, "delta": {"content": "{\\"files\\": '
    '[\\"/home/someone/work/a.md\\"]}"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'
)
TRACE = {
    "ts": "2026-09-22T10:00:00+00:00",
    "outcome": "rescued",
    "mode": "on",
    "stream": True,
    "model": "gpt-oss-120b",
    "tool_choice": "required",
    "tools": [PLUGIN_TOOL, STRUCTURED],
    "response": RESPONSE,
}


def test_fixture_records_the_expected_repair() -> None:
    fixture = build_fixture(TRACE, fixture_id="json-text", description="d")

    assert fixture["expected"] == {"outcome": "rescued", "tool": "StructuredOutput"}
    assert fixture["source"] == {
        "model": "gpt-oss-120b",
        "ts": "2026-09-22T10:00:00+00:00",
        "mode": "on",
    }
    assert replay(fixture) == ("rescued", "StructuredOutput")


def test_fixture_keeps_only_the_listed_tools() -> None:
    fixture = build_fixture(TRACE, fixture_id="x", description="d", keep_tools=["StructuredOutput"])

    assert [t["function"]["name"] for t in fixture["tools"]] == ["StructuredOutput"]


def test_unknown_kept_tool_is_an_error() -> None:
    with pytest.raises(ValueError, match="nope"):
        build_fixture(TRACE, fixture_id="x", description="d", keep_tools=["nope"])


def test_replacements_scrub_the_answer() -> None:
    fixture = build_fixture(
        TRACE, fixture_id="x", description="d", replacements=[("/home/someone/work", "/repo")]
    )

    assert "/home/someone" not in json.dumps(fixture)
    assert "/repo/a.md" in fixture["response"]


def test_promote_writes_a_file_and_refuses_to_overwrite(tmp_path: Path) -> None:
    traces = tmp_path / "traces.jsonl"
    traces.write_text(json.dumps(TRACE) + "\n")
    out = tmp_path / "fixtures"
    args = ["promote", str(traces), "--line", "1", "--id", "json-text", "--out-dir", str(out)]
    args += ["--description", "OpenCode answered with JSON text"]

    assert main(args) == 0
    fixture = json.loads((out / "json-text.json").read_text())
    assert fixture["id"] == "json-text"
    assert main(args) == 1


SPLIT_RESPONSE = (
    'data: {"id": "c1", "choices": [{"index": 0, "delta": {"role": "assistant", '
    '"reasoning_content": "List them."}, "finish_reason": null}]}\n\n'
    'data: {"id": "c1", "choices": [{"index": 0, "delta": {"content": "{\\"files\\": '
    '[\\"/home/some"}, "finish_reason": null}]}\n\n'
    'data: {"id": "c1", "choices": [{"index": 0, "delta": {"content": "one/work/a.md\\"]}"}, '
    '"finish_reason": "stop"}]}\n\n'
    'data: {"id": "c1", "choices": [], "usage": {"total_tokens": 9}}\n\n'
    "data: [DONE]\n\n"
)
SPLIT_TRACE = {**TRACE, "response": SPLIT_RESPONSE}


def test_replacement_split_across_stream_chunks_is_refused() -> None:
    with pytest.raises(ValueError, match="--coalesce"):
        build_fixture(
            SPLIT_TRACE,
            fixture_id="x",
            description="d",
            replacements=[("/home/someone/work", "/repo")],
        )


def test_coalesce_merges_stream_text_so_replacements_apply() -> None:
    fixture = build_fixture(
        SPLIT_TRACE,
        fixture_id="x",
        description="d",
        replacements=[("/home/someone/work", "/repo")],
        coalesce=True,
    )

    assert "someone" not in fixture["response"]
    assert "/repo/a.md" in fixture["response"]
    assert "List them." in fixture["response"], "reasoning is kept"
    assert '"total_tokens": 9' in fixture["response"], "usage is kept"
    assert fixture["expected"] == {"outcome": "rescued", "tool": "StructuredOutput"}
