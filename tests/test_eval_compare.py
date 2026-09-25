"""Tests for model comparison in the eval harness: auth, tokens, cost (no network)."""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

from evals import tool_choice_eval
from evals.scenarios import SCENARIOS
from evals.tool_choice_eval import DirectAuth, run_once, summarize_cost
from gpt_oss_shim.usage import PriceTable

SCENARIO = next(s for s in SCENARIOS if s["id"] == "forced-weather")
PRICES = PriceTable({"m": (1.0, 2.0)})


def _answer(request: httpx.Request) -> httpx.Response:
    call = {
        "id": "c",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "Lisbon"}'},
    }
    return httpx.Response(
        200,
        json={
            "model": "m",
            "choices": [{"index": 0, "message": {"tool_calls": [call]}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        },
    )


def test_direct_auth_prefers_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "k")

    assert DirectAuth().headers() == {"api-key": "k"}


def test_direct_auth_caches_an_entra_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_FOUNDRY_API_KEY", raising=False)
    calls: list[str] = []

    class FakeCredential:
        def get_token(self, scope: str) -> Any:
            calls.append(scope)
            return SimpleNamespace(token=f"t{len(calls)}", expires_on=10_000_000_000)

    fake = ModuleType("azure.identity")
    fake.DefaultAzureCredential = FakeCredential  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure.identity", fake)
    auth = DirectAuth()

    assert auth.headers() == {"Authorization": "Bearer t1"}
    assert auth.headers() == {"Authorization": "Bearer t1"}
    assert len(calls) == 1


def test_run_once_records_model_tokens_and_cost() -> None:
    with httpx.Client(transport=httpx.MockTransport(_answer)) as client:
        record = run_once(
            client,
            "shim",
            "http://shim.test/v1/chat/completions",
            SCENARIO,
            "m",
            prices=PRICES,
            label="shim:m",
        )

    assert record["target"] == "shim:m"
    assert record["model"] == "m"
    assert (record["input_tokens"], record["output_tokens"]) == (1000, 500)
    assert record["cost_usd"] == pytest.approx(0.002)
    assert record["ok"] is True and record["args"] is True


def test_run_once_sends_direct_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "k")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _answer(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_once(
            client, "direct", "http://azure.test/v1/chat/completions", SCENARIO, "m", prices=PRICES
        )

    assert seen[0].headers["api-key"] == "k"
    assert json.loads(seen[0].content)["model"] == "m"


def test_cost_summary_per_request_and_per_success() -> None:
    records = [
        {"target": "a", "ok": True, "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001},
        {"target": "a", "ok": False, "input_tokens": 300, "output_tokens": 150, "cost_usd": 0.003},
        {"target": "b", "ok": False, "input_tokens": None, "output_tokens": None, "cost_usd": None},
        {"target": "c", "ok": False, "input_tokens": 10, "output_tokens": 5, "cost_usd": None},
    ]

    lines = summarize_cost(records).splitlines()

    assert lines[2] == "| a | 200 | 100 | 2.000 | 0.00400 |"
    assert lines[3] == "| b | — | — | — | — |"
    assert lines[4] == "| c | 10 | 5 | — | — |"


def test_main_runs_every_target_for_every_model(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    seen: list[tuple[str, str]] = []

    def fake_run_once(client, target, url, scenario, model, **kwargs):  # type: ignore[no-untyped-def]
        seen.append((kwargs["label"], model))
        return {
            "target": kwargs["label"],
            "model": model,
            "ok": True,
            "progress": True,
            "reason": "ok",
            "outcome": "native",
            "seconds": 0.1,
            "args": None,
            "input_tokens": 1,
            "output_tokens": 1,
            "cost_usd": 0.0,
        }

    monkeypatch.setattr(tool_choice_eval, "run_once", fake_run_once)
    monkeypatch.setattr(tool_choice_eval, "SCENARIOS", [SCENARIO])

    assert tool_choice_eval.main(["--target", "shim=http://x", "--model", "a, b"]) == 0
    assert seen == [("shim:a", "a"), ("shim:b", "b")]
    assert "Est. USD / success" in capsys.readouterr().out
