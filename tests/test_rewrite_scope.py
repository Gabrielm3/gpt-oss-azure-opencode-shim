"""The tool_choice rewrite and the polyfill apply only to models that need them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from gpt_oss_shim.shim import _load_config, rewrites_model

TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


def forced(model: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": "weather in Lisbon"}],
        "tools": [TOOL],
        "tool_choice": "required",
    }
    if model is not None:
        body["model"] = model
    return body


def recording_handler(seen: list[dict[str, Any]]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"index": 0, "message": {"content": "sunny"}}]}
        )

    return handler


@pytest.mark.parametrize(
    ("model", "patterns", "expected"),
    [
        ("gpt-oss-120b", ("gpt-oss*",), True),
        ("GPT-OSS-20B", ("gpt-oss*",), True),
        ("gpt-5-mini", ("gpt-oss*",), False),
        ("my-oss-deploy", ("gpt-oss*", "my-oss-*"), True),
        ("gpt-5-mini", ("*",), True),
        (None, ("gpt-oss*",), True),
        (42, ("gpt-oss*",), True),
    ],
)
def test_rewrites_model(model: object, patterns: tuple[str, ...], expected: bool) -> None:
    assert rewrites_model(model, patterns) is expected


async def test_other_models_keep_their_forced_tool_choice(shim_client) -> None:
    seen: list[dict[str, Any]] = []
    async with shim_client(recording_handler(seen)) as client:
        response = await client.post("/v1/chat/completions", json=forced("gpt-5-mini"))

    assert seen[0]["tool_choice"] == "required"
    assert response.headers["x-shim-outcome"] == "passthrough"
    assert response.json()["choices"][0]["message"]["content"] == "sunny"


async def test_gpt_oss_is_still_rewritten_and_polyfilled(shim_client) -> None:
    seen: list[dict[str, Any]] = []
    async with shim_client(recording_handler(seen)) as client:
        response = await client.post("/v1/chat/completions", json=forced("gpt-oss-120b"))

    assert seen[0]["tool_choice"] == "auto"
    assert response.headers["x-shim-outcome"] == "failed"


async def test_request_without_model_is_rewritten(shim_client) -> None:
    seen: list[dict[str, Any]] = []
    async with shim_client(recording_handler(seen)) as client:
        await client.post("/v1/chat/completions", json=forced(None))

    assert seen[0]["tool_choice"] == "auto"


async def test_star_restores_rewriting_every_model(shim_client) -> None:
    seen: list[dict[str, Any]] = []
    async with shim_client(recording_handler(seen), rewrite_models=("*",)) as client:
        await client.post("/v1/chat/completions", json=forced("gpt-5-mini"))

    assert seen[0]["tool_choice"] == "auto"


async def test_outcome_log_does_not_count_unscoped_models_as_forced(
    shim_client, tmp_path: Path
) -> None:
    async with shim_client(recording_handler([]), trace_dir=tmp_path) as client:
        await client.post("/v1/chat/completions", json=forced("gpt-5-mini"))

    [line] = next(tmp_path.glob("outcomes-*.jsonl")).read_text().splitlines()
    assert json.loads(line)["forced"] is False


def test_rewrite_models_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPSTREAM_URL", "https://upstream.test/openai")
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "k")
    monkeypatch.delenv("SHIM_REWRITE_MODELS", raising=False)
    assert _load_config()["rewrite_models"] == ("gpt-oss*",)

    monkeypatch.setenv("SHIM_REWRITE_MODELS", " gpt-oss* , My-OSS-* ,")
    assert _load_config()["rewrite_models"] == ("gpt-oss*", "my-oss-*")
