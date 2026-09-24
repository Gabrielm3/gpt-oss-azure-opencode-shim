"""Tests for OpenTelemetry spans."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from gpt_oss_shim.polyfill import PolyfillMode
from gpt_oss_shim.shim import create_app
from gpt_oss_shim.telemetry import build_tracer, response_summary
from tests.conftest import FAKE_CONFIG, SHIM_BASE_URL

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
    "messages": [{"role": "user", "content": "which files?"}],
    "tools": [STRUCTURED],
    "tool_choice": "required",
}
USAGE = {"prompt_tokens": 137, "completion_tokens": 42, "total_tokens": 179}


class Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body


def _completion(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "model": "gpt-oss-120b-2026",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": USAGE,
    }


def _sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter, provider.get_tracer("test")
    provider.shutdown()


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=SHIM_BASE_URL)


def _app(spans, handler, **overrides):
    _, tracer = spans
    return create_app(
        {**FAKE_CONFIG, **overrides}, transport=httpx.MockTransport(handler), tracer=tracer
    )


def _attributes(exporter: InMemorySpanExporter) -> dict[str, Any]:
    [span] = exporter.get_finished_spans()
    return dict(span.attributes)


# --- response_summary ---------------------------------------------------------


def test_summary_of_a_completion() -> None:
    body = json.dumps(_completion({"role": "assistant", "content": "hi"})).encode()

    assert response_summary(body, is_sse=False) == {
        "response_id": "chatcmpl-1",
        "response_model": "gpt-oss-120b-2026",
        "input_tokens": 137,
        "output_tokens": 42,
        "finish_reasons": ["stop"],
    }


def test_summary_of_a_stream() -> None:
    body = _sse(
        {"id": "chatcmpl-2", "model": "m", "choices": [{"index": 0, "delta": {"content": "x"}}]},
        {"id": "chatcmpl-2", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"id": "chatcmpl-2", "choices": [], "usage": USAGE},
    )

    assert response_summary(body, is_sse=True) == {
        "response_id": "chatcmpl-2",
        "response_model": "m",
        "input_tokens": 137,
        "output_tokens": 42,
        "finish_reasons": ["tool_calls"],
    }


def test_summary_of_an_unparseable_body() -> None:
    assert response_summary(b"<html>", is_sse=False) == {}


# --- spans --------------------------------------------------------------------


async def test_rescued_request_produces_one_chat_span(spans) -> None:
    exporter, _ = spans
    completion = _completion({"role": "assistant", "content": '{"files": ["a.md"]}'})
    app = _app(spans, lambda request: httpx.Response(200, json=completion))

    async with _client(app) as client:
        await client.post("/v1/chat/completions", json=FORCED_REQUEST)

    [span] = exporter.get_finished_spans()
    assert span.name == "chat gpt-oss-120b"
    attributes = dict(span.attributes)
    assert attributes["gen_ai.operation.name"] == "chat"
    assert attributes["gen_ai.request.model"] == "gpt-oss-120b"
    assert attributes["gen_ai.response.model"] == "gpt-oss-120b-2026"
    assert attributes["gen_ai.usage.input_tokens"] == 137
    assert attributes["gen_ai.usage.output_tokens"] == 42
    assert attributes["gen_ai.response.finish_reasons"] == ("stop",), (
        "gen_ai.* describe the upstream answer; shim.outcome says what the client got"
    )
    assert attributes["shim.outcome"] == "rescued"
    assert attributes["shim.polyfill.mode"] == "on"
    assert attributes["shim.tool_choice.forced"] is True
    assert attributes["http.response.status_code"] == 200


async def test_spans_never_carry_the_upstream_host(spans) -> None:
    exporter, _ = spans
    app = _app(spans, lambda request: httpx.Response(200, json=_completion({"content": "hi"})))

    async with _client(app) as client:
        await client.post("/v1/chat/completions", json=FORCED_REQUEST)

    assert "upstream.test" not in json.dumps(_attributes(exporter), default=str)


async def test_streamed_passthrough_is_traced_once(spans) -> None:
    exporter, _ = spans
    body = _sse({"id": "c", "choices": [{"index": 0, "delta": {"content": "hi"}}]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream(body)
        )

    app = _app(spans, handler)
    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions", json={"model": "m", "messages": [], "stream": True}
        )

    assert response.content == body
    assert _attributes(exporter)["shim.outcome"] == "passthrough"


async def test_upstream_failure_is_recorded_on_the_span(spans) -> None:
    exporter, _ = spans

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    app = _app(spans, handler)
    async with _client(app) as client:
        await client.post("/v1/chat/completions", json=FORCED_REQUEST)

    [span] = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert dict(span.attributes)["error.type"] == "ConnectTimeout"
    assert dict(span.attributes)["shim.outcome"] == "upstream_error"


async def test_observe_mode_records_what_it_would_have_done(spans) -> None:
    exporter, _ = spans
    body = _sse(
        {"id": "c", "choices": [{"index": 0, "delta": {"content": '{"files": ["a.md"]}'}}]},
        {"id": "c", "choices": [], "usage": USAGE},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream(body)
        )

    app = _app(spans, handler, tool_polyfill=PolyfillMode.OBSERVE)
    async with _client(app) as client:
        await client.post("/v1/chat/completions", json={**FORCED_REQUEST, "stream": True})

    attributes = _attributes(exporter)
    assert attributes["shim.outcome"] == "rewritten"
    assert attributes["shim.observed_outcome"] == "rescued"
    assert attributes["gen_ai.usage.input_tokens"] == 137


async def test_requests_that_are_not_chat_completions_are_not_traced(spans) -> None:
    exporter, _ = spans
    app = _app(spans, lambda request: httpx.Response(200, json={"data": []}))

    async with _client(app) as client:
        await client.get("/v1/models")

    assert exporter.get_finished_spans() == ()


# --- build_tracer -------------------------------------------------------------

_OTEL_VARS = (
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
)


@pytest.fixture
def otel_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in _OTEL_VARS:
        monkeypatch.delenv(name, raising=False)
    shutdowns: list[object] = []
    monkeypatch.setattr("atexit.register", shutdowns.append)
    return monkeypatch


def test_tracing_is_off_without_configuration(otel_env: pytest.MonkeyPatch) -> None:
    assert build_tracer("1.0") is None


def test_exporter_none_wins_over_an_endpoint(otel_env: pytest.MonkeyPatch) -> None:
    otel_env.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    otel_env.setenv("OTEL_TRACES_EXPORTER", "none")

    assert build_tracer("1.0") is None


def test_console_exporter_builds_a_tracer(otel_env: pytest.MonkeyPatch) -> None:
    otel_env.setenv("OTEL_TRACES_EXPORTER", "console")

    assert build_tracer("1.0") is not None


def test_missing_otlp_exporter_warns_and_disables_tracing(
    otel_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    otel_env.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    otel_env.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.http.trace_exporter", None)

    assert build_tracer("1.0") is None
    assert "gpt-oss-azure-opencode-shim[otel]" in caplog.text
