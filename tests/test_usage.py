"""Tests for token usage, estimated cost and streaming latency."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from gpt_oss_shim.metrics import MAX_MODEL_LABELS, Metrics
from gpt_oss_shim.polyfill import PolyfillMode
from gpt_oss_shim.report import summarize_usage
from gpt_oss_shim.usage import PriceTable, SseMeter, Usage, usage_from_summary

USAGE = {"prompt_tokens": 1000, "completion_tokens": 500}
PRICES = PriceTable({"gpt-oss-120b": (0.15, 0.60)})
COST = (1000 * 0.15 + 500 * 0.60) / 1_000_000

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
PLAIN = {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "hi"}]}
FORCED = {**PLAIN, "tools": [STRUCTURED], "tool_choice": "required"}


def _event(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


STREAM_EVENTS = [
    _event({"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}),
    _event({"choices": [{"index": 0, "delta": {"reasoning_content": "thinking"}}]}),
    _event({"choices": [{"index": 0, "delta": {"content": '{"files": ["a.md"]}'}}]}),
    _event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": USAGE}),
    b"data: [DONE]\n\n",
]


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


def sse_handler(chunks: list[bytes]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream(chunks)
        )

    return handler


def json_handler(body: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return handler


def completion(content: str, usage: dict[str, int] | None = USAGE) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "c1",
        "model": "gpt-oss-120b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def metric(text: str, name: str, **labels: str) -> float:
    """Value of one sample in a Prometheus text exposition, 0 if absent."""
    rendered = ",".join(f'{k}="{v}"' for k, v in labels.items())
    prefix = f"{name}{{{rendered}}} " if labels else f"{name} "
    for line in text.splitlines():
        if line.startswith(prefix):
            return float(line.split()[-1])
    return 0.0


# --- prices -------------------------------------------------------------------


def test_cost_is_tokens_times_list_price() -> None:
    assert PRICES.cost("gpt-oss-120b", Usage(1000, 500)) == pytest.approx(COST)


def test_model_without_a_price_has_no_cost() -> None:
    assert PRICES.cost("mystery", Usage(1, 1)) is None


def test_env_prices_override_and_extend_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_PRICES", "gpt-oss-120b=1/2, custom = 3/4 ,")

    table = PriceTable.from_env()

    assert table.cost("gpt-oss-120b", Usage(1_000_000, 1_000_000)) == 3
    assert table.cost("custom", Usage(1_000_000, 0)) == 3
    assert table.cost("gpt-5-mini", Usage(1_000_000, 0)) == 0.25


@pytest.mark.parametrize("raw", ["gpt-oss-120b", "m=1", "m=a/b", "m=-1/2"])
def test_bad_env_prices_fail_fast(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SHIM_PRICES", raw)

    with pytest.raises(RuntimeError, match="SHIM_PRICES"):
        PriceTable.from_env()


@pytest.mark.parametrize(
    "summary",
    [None, {}, {"input_tokens": 1}, {"input_tokens": "1", "output_tokens": 2}],
)
def test_incomplete_usage_is_none(summary: dict[str, Any] | None) -> None:
    assert usage_from_summary(summary) is None


# --- stream meter -------------------------------------------------------------


def test_meter_finds_first_token_and_usage_across_split_chunks() -> None:
    now = [10.0]
    meter = SseMeter(started=10.0, clock=lambda: now[0])
    body = b"".join(STREAM_EVENTS)

    meter.feed(body[:30])  # role-only event, cut mid-line: no token yet
    now[0] = 10.8
    for i in range(30, len(body), 7):
        meter.feed(body[i : i + 7])
    meter.close()

    assert meter.time_to_first_token == pytest.approx(0.8)
    assert meter.usage == Usage(1000, 500)
    assert meter.output_tokens_per_second(finished=10.8 + 5) == pytest.approx(100)


def test_meter_ignores_noise_and_handles_a_stream_without_tokens() -> None:
    meter = SseMeter(started=0.0, clock=lambda: 1.0)

    meter.feed(b": keep-alive\n\ndata: not json\n\ndata: [1]\n\ndata: [DONE]")
    meter.close()

    assert meter.time_to_first_token is None
    assert meter.usage is None
    assert meter.output_tokens_per_second(finished=2.0) is None


def test_zero_generation_time_has_no_rate() -> None:
    meter = SseMeter(started=0.0, clock=lambda: 1.0)
    meter.feed(b"".join(STREAM_EVENTS))

    assert meter.output_tokens_per_second(finished=1.0) is None


# --- metrics ------------------------------------------------------------------


def test_model_label_is_bounded() -> None:
    metrics = Metrics()
    labels = [metrics.model_label(f"m{i}") for i in range(MAX_MODEL_LABELS + 3)]

    assert labels[-1] == "other"
    assert len(set(labels)) == MAX_MODEL_LABELS + 1
    assert metrics.model_label("m0") == "m0"
    assert metrics.model_label(None) == "other"


# --- through the app ----------------------------------------------------------


async def test_json_answer_counts_tokens_and_cost(shim_client, tmp_path: Path) -> None:
    async with shim_client(
        json_handler(completion("hello")), prices=PRICES, trace_dir=tmp_path
    ) as client:
        await client.post("/v1/chat/completions", json=PLAIN)
        text = (await client.get("/metrics")).text

    assert metric(text, "shim_tokens_total", model="gpt-oss-120b", type="input") == 1000
    assert metric(text, "shim_tokens_total", model="gpt-oss-120b", type="output") == 500
    assert metric(text, "shim_cost_usd_total", model="gpt-oss-120b") == pytest.approx(COST)
    [record] = _outcomes(tmp_path)
    assert (record["input_tokens"], record["output_tokens"]) == (1000, 500)
    assert record["cost_usd"] == pytest.approx(COST)


async def test_passthrough_stream_counts_tokens_ttft_and_rate(shim_client, tmp_path: Path) -> None:
    async with shim_client(sse_handler(STREAM_EVENTS), prices=PRICES, trace_dir=tmp_path) as client:
        response = await client.post("/v1/chat/completions", json={**PLAIN, "stream": True})
        text = (await client.get("/metrics")).text

    assert response.content == b"".join(STREAM_EVENTS)
    assert metric(text, "shim_tokens_total", model="gpt-oss-120b", type="output") == 500
    assert metric(text, "shim_time_to_first_token_seconds_count", model="gpt-oss-120b") == 1
    assert metric(text, "shim_output_tokens_per_second_count", model="gpt-oss-120b") == 1
    [record] = _outcomes(tmp_path)
    assert record["output_tokens"] == 500
    assert record["stream"] is True


async def test_forced_buffered_answer_counts_tokens_without_ttft(shim_client) -> None:
    async with shim_client(json_handler(completion('{"files": ["a.md"]}'))) as client:
        response = await client.post("/v1/chat/completions", json=FORCED)
        text = (await client.get("/metrics")).text

    assert response.headers["x-shim-outcome"] == "rescued"
    assert metric(text, "shim_tokens_total", model="gpt-oss-120b", type="input") == 1000
    assert metric(text, "shim_time_to_first_token_seconds_count", model="gpt-oss-120b") == 0


async def test_forced_stream_is_buffered_and_counts_tokens(shim_client) -> None:
    async with shim_client(sse_handler(STREAM_EVENTS)) as client:
        response = await client.post("/v1/chat/completions", json={**FORCED, "stream": True})
        text = (await client.get("/metrics")).text

    assert response.headers["x-shim-outcome"] == "rescued"
    assert metric(text, "shim_tokens_total", model="gpt-oss-120b", type="output") == 500


async def test_observe_stream_counts_tokens(shim_client) -> None:
    async with shim_client(
        sse_handler(STREAM_EVENTS), tool_polyfill=PolyfillMode.OBSERVE
    ) as client:
        await client.post("/v1/chat/completions", json={**FORCED, "stream": True})
        text = (await client.get("/metrics")).text

    assert metric(text, "shim_tokens_total", model="gpt-oss-120b", type="output") == 500
    assert metric(text, "shim_time_to_first_token_seconds_count", model="gpt-oss-120b") == 1


async def test_empty_choices_still_counts_billed_tokens(shim_client) -> None:
    body = {"id": "c1", "choices": [], "usage": USAGE}
    async with shim_client(json_handler(body)) as client:
        response = await client.post("/v1/chat/completions", json=PLAIN)
        text = (await client.get("/metrics")).text

    assert response.status_code == 502
    assert metric(text, "shim_tokens_total", model="gpt-oss-120b", type="input") == 1000


async def test_answer_without_usage_is_counted_as_missing(shim_client) -> None:
    async with shim_client(json_handler(completion("hi", usage=None))) as client:
        await client.post("/v1/chat/completions", json=PLAIN)
        text = (await client.get("/metrics")).text

    assert metric(text, "shim_usage_missing_total", model="gpt-oss-120b") == 1
    assert metric(text, "shim_tokens_total", model="gpt-oss-120b", type="input") == 0


async def test_non_chat_requests_are_not_metered(shim_client) -> None:
    async with shim_client(json_handler({"data": []})) as client:
        await client.get("/v1/models")
        text = (await client.get("/metrics")).text

    assert "shim_usage_missing_total{" not in text


async def test_a_failing_meter_never_breaks_the_stream(
    shim_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: SseMeter, chunk: bytes) -> None:
        raise ValueError("boom")

    monkeypatch.setattr(SseMeter, "feed", broken)
    async with shim_client(sse_handler(STREAM_EVENTS)) as client:
        response = await client.post("/v1/chat/completions", json={**PLAIN, "stream": True})

    assert response.content == b"".join(STREAM_EVENTS)


# --- report -------------------------------------------------------------------


def test_report_sums_tokens_and_cost_per_day() -> None:
    records = [
        {
            "ts": "2026-09-20T10:00:00+00:00",
            "input_tokens": 10,
            "output_tokens": 5,
            "cost_usd": 0.5,
        },
        {"ts": "2026-09-20T11:00:00+00:00"},
        {
            "ts": "2026-09-21T09:00:00+00:00",
            "input_tokens": 1000,
            "output_tokens": 1,
            "cost_usd": 1,
        },
    ]

    table = summarize_usage(records)

    assert "| 2026-09-20 | 2 | 1 | 10 | 5 | 0.5000 |" in table
    assert "| 2026-09-21 | 1 | 1 | 1,000 | 1 | 1.0000 |" in table
    assert "| **Total** | 3 | 2 | 1,010 | 6 | 1.5000 |" in table


def _outcomes(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for f in sorted(directory.glob("outcomes-*.jsonl"))
        for line in f.read_text().splitlines()
    ]
