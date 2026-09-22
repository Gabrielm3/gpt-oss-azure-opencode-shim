"""End-to-end app tests for the tool-call polyfill, outcome header and metrics."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

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
FORCED_REQUEST = {
    "model": "gpt-oss-120b",
    "messages": [{"role": "user", "content": "Which markdown files are here?"}],
    "tools": [STRUCTURED],
    "tool_choice": "required",
}


class Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body


def _sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"id": "c1", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _completion(message: dict[str, Any]) -> dict[str, Any]:
    return {"id": "c1", "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


def _tool_calls_in_sse(body: bytes) -> list[dict[str, Any]]:
    calls = []
    for line in body.decode().splitlines():
        if line.startswith("data: {"):
            for choice in json.loads(line[len("data: ") :]).get("choices", []):
                calls.extend((choice.get("delta") or {}).get("tool_calls") or [])
    return calls


def json_text_stream(request: httpx.Request) -> httpx.Response:
    body = _sse(_chunk({"content": '{"files": ["a.md"]}'}, "stop"))
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream(body))


async def test_forced_stream_answered_with_json_text_is_rescued(shim_client) -> None:
    async with shim_client(json_text_stream) as client:
        response = await client.post(
            "/v1/chat/completions", json={**FORCED_REQUEST, "stream": True}
        )

    assert response.status_code == 200
    assert response.headers["x-shim-outcome"] == "rescued"
    calls = _tool_calls_in_sse(response.content)
    assert [c["function"]["name"] for c in calls] == ["StructuredOutput"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"files": ["a.md"]}


async def test_forced_completion_with_native_tool_call_is_marked_native(shim_client) -> None:
    call = {
        "id": "x",
        "type": "function",
        "function": {"name": "StructuredOutput", "arguments": "{}"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion({"role": "assistant", "tool_calls": [call]}))

    async with shim_client(handler) as client:
        response = await client.post("/v1/chat/completions", json=FORCED_REQUEST)

    assert response.headers["x-shim-outcome"] == "native"
    assert response.json()["choices"][0]["message"]["tool_calls"] == [call]


async def test_polyfill_can_be_disabled(shim_client) -> None:
    async with shim_client(json_text_stream, tool_polyfill=False) as client:
        response = await client.post(
            "/v1/chat/completions", json={**FORCED_REQUEST, "stream": True}
        )

    assert response.headers["x-shim-outcome"] == "rewritten"
    assert _tool_calls_in_sse(response.content) == []


async def test_unforced_stream_is_passed_through(shim_client) -> None:
    body = _sse(_chunk({"content": "hello"}, "stop"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream(body)
        )

    async with shim_client(handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [], "stream": True},
        )

    assert response.headers["x-shim-outcome"] == "passthrough"
    assert response.content == body


async def test_completion_with_empty_choices_becomes_an_explicit_error(shim_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "c1", "choices": [], "usage": {"total_tokens": 2}})

    async with shim_client(handler) as client:
        response = await client.post("/v1/chat/completions", json={"model": "m", "messages": []})

    assert response.status_code == 502
    assert response.headers["x-shim-outcome"] == "empty_choices"
    assert response.json()["error"]["type"] == "empty_choices"


async def test_upstream_failure_is_marked_in_header(shim_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with shim_client(handler) as client:
        response = await client.post("/v1/chat/completions", json=FORCED_REQUEST)

    assert response.status_code == 502
    assert response.headers["x-shim-outcome"] == "upstream_error"


async def test_metrics_count_outcomes(shim_client) -> None:
    async with shim_client(json_text_stream) as client:
        await client.post("/v1/chat/completions", json={**FORCED_REQUEST, "stream": True})
        metrics = await client.get("/metrics")

    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    text = metrics.text
    assert 'shim_requests_total{outcome="rescued"} 1.0' in text
    assert 'shim_requests_total{outcome="failed"} 0.0' in text
    assert 'shim_forced_request_duration_seconds_count{outcome="rescued"} 1.0' in text


async def test_metrics_are_isolated_per_app(shim_client) -> None:
    async with shim_client(json_text_stream) as client:
        await client.post("/v1/chat/completions", json={**FORCED_REQUEST, "stream": True})

    async with shim_client() as client:
        text = (await client.get("/metrics")).text

    assert 'shim_requests_total{outcome="rescued"} 0.0' in text
