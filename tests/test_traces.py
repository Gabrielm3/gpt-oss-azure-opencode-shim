"""Tests for polyfill traces: what gets recorded, where, and what never does."""

from __future__ import annotations

import json
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from gpt_oss_shim.polyfill import Outcome, PolyfillMode
from gpt_oss_shim.traces import TraceWriter

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
SECRET_PROMPT = "my private prompt with an internal hostname"
FORCED_REQUEST = {
    "model": "gpt-oss-120b",
    "messages": [{"role": "user", "content": SECRET_PROMPT}],
    "tools": [STRUCTURED],
    "tool_choice": "required",
    "stream": True,
}
JSON_TEXT_SSE = (
    b'data: {"id": "c1", "choices": [{"index": 0, "delta": {"content": "{\\"files\\": '
    b'[\\"a.md\\"]}"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'
)


class Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body


def sse_handler(body: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream(body)
        )

    return handler


def _records(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for f in sorted(directory.glob("traces-*.jsonl"))
        for line in f.read_text().splitlines()
    ]


# --- TraceWriter -------------------------------------------------------------


def test_writer_creates_private_directory_and_file(tmp_path: Path) -> None:
    directory = tmp_path / "traces"
    writer = TraceWriter(directory, frozenset({Outcome.FAILED}))

    writer.write(
        outcome=Outcome.FAILED,
        mode=PolyfillMode.ON,
        stream=False,
        request={"model": "m", "tools": [STRUCTURED], "tool_choice": "required"},
        response=b'{"choices": []}',
    )

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    [trace_file] = directory.glob("traces-*.jsonl")
    assert stat.S_IMODE(trace_file.stat().st_mode) == 0o600
    [record] = _records(directory)
    assert record["outcome"] == "failed"
    assert record["mode"] == "on"
    assert record["response"] == '{"choices": []}'


def test_writer_skips_outcomes_it_was_not_asked_to_keep(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path, frozenset({Outcome.FAILED}))

    writer.write(
        outcome=Outcome.NATIVE,
        mode=PolyfillMode.ON,
        stream=False,
        request={"model": "m", "tools": [], "tool_choice": "required"},
        response=b"{}",
    )

    assert _records(tmp_path) == []


def test_writer_never_records_messages(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path, frozenset({Outcome.FAILED}))

    writer.write(
        outcome=Outcome.FAILED,
        mode=PolyfillMode.ON,
        stream=False,
        request={**FORCED_REQUEST, "stream": False},
        response=b"{}",
    )

    text = "".join(f.read_text() for f in tmp_path.glob("*.jsonl"))
    assert SECRET_PROMPT not in text
    assert "messages" not in _records(tmp_path)[0]


def test_writer_failure_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    writer = TraceWriter(blocker / "traces", frozenset({Outcome.FAILED}))

    writer.write(
        outcome=Outcome.FAILED,
        mode=PolyfillMode.ON,
        stream=False,
        request={"model": "m", "tools": [], "tool_choice": "required"},
        response=b"{}",
    )


# --- app integration ----------------------------------------------------------


async def test_rescued_stream_is_traced_with_the_raw_upstream_answer(
    shim_client, tmp_path: Path
) -> None:
    async with shim_client(sse_handler(JSON_TEXT_SSE), trace_dir=tmp_path) as client:
        response = await client.post("/v1/chat/completions", json=FORCED_REQUEST)

    assert response.headers["x-shim-outcome"] == "rescued"
    [record] = _records(tmp_path)
    assert record["outcome"] == "rescued"
    assert record["stream"] is True
    assert record["model"] == "gpt-oss-120b"
    assert record["tool_choice"] == "required"
    assert record["tools"] == [STRUCTURED]
    assert record["response"] == JSON_TEXT_SSE.decode(), "the answer before the repair"
    assert SECRET_PROMPT not in json.dumps(record)


async def test_observe_mode_traces_what_it_evaluated(shim_client, tmp_path: Path) -> None:
    async with shim_client(
        sse_handler(JSON_TEXT_SSE), trace_dir=tmp_path, tool_polyfill=PolyfillMode.OBSERVE
    ) as client:
        await client.post("/v1/chat/completions", json=FORCED_REQUEST)

    [record] = _records(tmp_path)
    assert record["outcome"] == "rescued"
    assert record["mode"] == "observe"


async def test_nothing_is_traced_without_a_trace_dir(shim_client, tmp_path: Path) -> None:
    async with shim_client(sse_handler(JSON_TEXT_SSE)) as client:
        await client.post("/v1/chat/completions", json=FORCED_REQUEST)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("kept", "expected"),
    [(frozenset({Outcome.FAILED}), 0), (frozenset({Outcome.RESCUED, Outcome.NATIVE}), 1)],
)
async def test_trace_outcomes_filter_what_is_kept(
    shim_client, tmp_path: Path, kept: frozenset[Outcome], expected: int
) -> None:
    async with shim_client(
        sse_handler(JSON_TEXT_SSE), trace_dir=tmp_path, trace_outcomes=kept
    ) as client:
        await client.post("/v1/chat/completions", json=FORCED_REQUEST)

    assert len(_records(tmp_path)) == expected


# --- outcome log --------------------------------------------------------------


def _outcomes(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for f in sorted(directory.glob("outcomes-*.jsonl"))
        for line in f.read_text().splitlines()
    ]


async def test_every_chat_request_lands_in_the_outcome_log(shim_client, tmp_path: Path) -> None:
    plain = {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": SECRET_PROMPT}]}
    async with shim_client(sse_handler(JSON_TEXT_SSE), trace_dir=tmp_path) as client:
        await client.post("/v1/chat/completions", json=FORCED_REQUEST)
        await client.post("/v1/chat/completions", json={**plain, "stream": True})
        await client.get("/v1/models")

    forced, plain_record = _outcomes(tmp_path)
    assert forced["outcome"] == "rescued"
    assert forced["forced"] is True
    assert forced["stream"] is True
    assert forced["status"] == 200
    assert forced["seconds"] >= 0
    assert plain_record["outcome"] == "passthrough"
    assert plain_record["forced"] is False
    assert SECRET_PROMPT not in (tmp_path / next(tmp_path.glob("outcomes-*")).name).read_text()


async def test_forced_request_is_marked_forced_even_with_the_polyfill_off(
    shim_client, tmp_path: Path
) -> None:
    async with shim_client(
        sse_handler(JSON_TEXT_SSE), trace_dir=tmp_path, tool_polyfill=PolyfillMode.OFF
    ) as client:
        await client.post("/v1/chat/completions", json=FORCED_REQUEST)

    [record] = _outcomes(tmp_path)
    assert record["outcome"] == "rewritten"
    assert record["forced"] is True
    assert record["mode"] == "off"


async def test_dropped_traces_are_counted_in_metrics(shim_client, tmp_path: Path) -> None:
    async with shim_client(
        sse_handler(JSON_TEXT_SSE), trace_dir=tmp_path, trace_max_bytes=10
    ) as client:
        await client.post("/v1/chat/completions", json=FORCED_REQUEST)
        metrics = (await client.get("/metrics")).text

    assert _records(tmp_path) == []
    assert "shim_traces_dropped_total 1.0" in metrics
