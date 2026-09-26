"""Tests for golden-argument scoring (no network)."""

from __future__ import annotations

import json

import pytest

from evals.gate import evaluate
from evals.golden import args_correct
from evals.scenarios import SCENARIOS
from evals.tool_choice_eval import golden, summarize_args


def _body(name: str, arguments: dict) -> bytes:
    call = {
        "id": "c",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return json.dumps({"choices": [{"index": 0, "message": {"tool_calls": [call]}}]}).encode()


@pytest.mark.parametrize(
    ("expect", "arguments", "correct"),
    [
        ({"age": ("exact", 34)}, {"age": 34}, True),
        ({"age": ("exact", 34)}, {"age": 34.0}, True),
        ({"age": ("exact", 34)}, {"age": "34"}, False),
        ({"ok": ("exact", True)}, {"ok": 1}, False),
        ({"city": ("text", "Recife")}, {"city": "  recife "}, True),
        ({"city": ("text", "Recife")}, {"city": 3}, False),
        ({"to": ("oneof", ["EUR", "euros"])}, {"to": "Euros"}, True),
        ({"to": ("oneof", ["EUR", "euros"])}, {"to": "GBP"}, False),
        ({"files": ("files", ["a.md", "b.md"])}, {"files": ["/r/b.md", "docs/A.md"]}, True),
        ({"files": ("files", ["a.md", "b.md"])}, {"files": ["a.md"]}, False),
        ({"files": ("files", ["a.md"])}, {"files": "a.md"}, False),
        (
            {"deps": ("names", ["x", "y"])},
            {"deps": [{"name": "Y"}, {"name": "x", "version": "1"}]},
            True,
        ),
        ({"deps": ("names", ["x", "y"])}, {"deps": ["x", "y"]}, False),
        ({"age": ("exact", 34)}, {}, False),
    ],
)
def test_matchers(expect: dict, arguments: dict, correct: bool) -> None:
    assert args_correct(expect, json.dumps(arguments)) is correct


def test_no_expectation_or_bad_json() -> None:
    assert args_correct(None, "{}") is None
    assert args_correct({"a": ("exact", 1)}, "not json") is False
    assert args_correct({"a": ("exact", 1)}, "[1]") is False


def test_every_golden_scenario_expects_fields_its_tool_declares() -> None:
    for scenario in SCENARIOS:
        expect = scenario.get("expect_args")
        if not expect:
            continue
        tool = next(
            t for t in scenario["tools"] if t["function"]["name"] == scenario["expect_tool"]
        )
        declared = tool["function"]["parameters"]["properties"]
        assert set(expect) <= set(declared), scenario["id"]


def test_golden_scores_only_strict_successes() -> None:
    scenario = next(s for s in SCENARIOS if s["id"] == "forced-weather")

    assert golden(scenario, 200, _body("get_weather", {"city": "Lisbon"}), stream=False) is True
    assert golden(scenario, 200, _body("get_weather", {"city": "Porto"}), stream=False) is False
    assert golden(scenario, 200, _body("search_docs", {"query": "x"}), stream=False) is None
    no_golden = next(s for s in SCENARIOS if s["id"] == "forced-search")
    assert golden(no_golden, 200, _body("search_docs", {"query": "x"}), stream=False) is None


def _record(outcome: str, args: bool | None) -> dict:
    return {
        "target": "shim",
        "ok": True,
        "reason": "ok",
        "outcome": outcome,
        "seconds": 1.0,
        "args": args,
    }


def test_summary_splits_native_and_rescued() -> None:
    records = [
        _record("native", True),
        _record("native", True),
        _record("rescued", True),
        _record("rescued", False),
        _record("native", None),
    ]

    row = summarize_args(records).splitlines()[2]

    assert row.startswith("| shim | 3/4 (75%")
    assert "| 2/2 (100%" in row
    assert "| 1/2 (50%" in row


def test_summary_without_scored_rows() -> None:
    assert summarize_args([_record("native", None)]) == "No golden-argument results."


def test_gate_report_includes_argument_values_without_gating_on_them() -> None:
    records = [_record("rescued", False) for _ in range(40)]
    baseline = {
        "source": "test",
        "metrics": {
            name: {"k": 0 if name == "stalled" else 40, "n": 40}
            for name in ("stalled", "success", "progress")
        },
    }

    result = evaluate(records, baseline, target="shim")

    assert "Argument values (reported, not gated)" in result.markdown
    assert "0/40" in result.markdown
    assert result.verdict.name == "PASS"
