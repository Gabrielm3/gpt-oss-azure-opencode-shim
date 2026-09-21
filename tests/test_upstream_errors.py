"""Tests for upstream failures, response headers and SSE relaying."""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator

import httpx
import pytest


class ChunkedStream(httpx.AsyncByteStream):
    """Upstream body that yields ``chunks`` and optionally fails afterwards.

    ``httpx.Response(content=...)`` is read eagerly, so streaming tests need
    a real ``AsyncByteStream`` to exercise ``aiter_*`` paths.
    """

    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


def sse_events(body: bytes) -> list[str]:
    """Return the ``data:`` payloads of an SSE body, in order."""
    return [
        line[len("data: ") :] for line in body.decode().splitlines() if line.startswith("data: ")
    ]


async def test_read_error_on_json_response_returns_502(shim_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        stream = ChunkedStream([b'{"partial'], httpx.ReadError("connection reset"))
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    async with shim_client(handler) as client:
        response = await client.post("/v1/chat/completions", json={"model": "m"})

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"


async def test_read_timeout_returns_504(shim_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        stream = ChunkedStream([], httpx.ReadTimeout("timed out"))
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    async with shim_client(handler) as client:
        response = await client.post("/v1/chat/completions", json={"model": "m"})

    assert response.status_code == 504
    assert response.json()["error"]["type"] == "upstream_timeout"


async def test_connect_timeout_returns_504(shim_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with shim_client(handler) as client:
        response = await client.post("/v1/chat/completions", json={"model": "m"})

    assert response.status_code == 504


async def test_rate_limit_response_keeps_status_body_and_headers(shim_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"code": "429", "message": "rate limited"}},
            headers={"retry-after": "7", "x-request-id": "req-123"},
        )

    async with shim_client(handler) as client:
        response = await client.post("/v1/chat/completions", json={"model": "m"})

    assert response.status_code == 429
    assert response.json()["error"]["message"] == "rate limited"
    assert response.headers["retry-after"] == "7"
    assert response.headers["x-request-id"] == "req-123"


async def test_compressed_upstream_body_is_decoded_without_content_encoding(shim_client) -> None:
    payload = json.dumps({"ok": True}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            stream=ChunkedStream([gzip.compress(payload)]),
        )

    async with shim_client(handler) as client:
        response = await client.get("/v1/models")

    assert "content-encoding" not in response.headers
    assert response.json() == {"ok": True}
    assert response.headers["content-length"] == str(len(payload))


async def test_sse_events_are_relayed_in_order(shim_client) -> None:
    chunks = [b'data: {"n": 1}\n\n', b'data: {"n": 2}\n\n', b"data: [DONE]\n\n"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "req-sse"},
            stream=ChunkedStream(chunks),
        )

    async with shim_client(handler) as client:
        response = await client.post("/v1/chat/completions", json={"stream": True})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-request-id"] == "req-sse"
    assert sse_events(response.content) == ['{"n": 1}', '{"n": 2}', "[DONE]"]


@pytest.mark.parametrize(
    "error",
    [httpx.ReadError("connection reset"), httpx.ReadTimeout("timed out")],
)
async def test_interrupted_sse_stream_ends_with_error_event(shim_client, error: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkedStream([b'data: {"n": 1}\n\n'], error),
        )

    async with shim_client(handler) as client:
        response = await client.post("/v1/chat/completions", json={"stream": True})

    events = sse_events(response.content)
    assert events[0] == '{"n": 1}'
    assert "[DONE]" not in events
    last = json.loads(events[-1])
    assert last["error"]["type"] in {"upstream_error", "upstream_timeout"}
