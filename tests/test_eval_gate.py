"""Tests for the live-eval gate: drift, pass and inconclusive verdicts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.gate import Verdict, evaluate, main

BASELINE: dict[str, Any] = {
    "source": "test",
    "metrics": {
        "stalled": {"k": 4, "n": 60},
        "success": {"k": 53, "n": 60},
        "progress": {"k": 56, "n": 60},
    },
}


def _records(*, ok: int, stalled: int, errors: int = 0, total: int = 60) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(total):
        if i < errors:
            rows.append({"target": "shim", "ok": False, "progress": False, "reason": "http_500"})
        elif i < errors + stalled:
            rows.append(
                {"target": "shim", "ok": False, "progress": False, "reason": "no_tool_call"}
            )
        elif i < errors + stalled + ok:
            rows.append({"target": "shim", "ok": True, "progress": True, "reason": "ok"})
        else:
            rows.append(
                {"target": "shim", "ok": False, "progress": True, "reason": "wrong_tool:read"}
            )
    return rows


def test_same_rates_as_baseline_pass() -> None:
    result = evaluate(_records(ok=53, stalled=4), BASELINE, target="shim")

    assert result.verdict is Verdict.PASS


def test_a_clear_rise_in_stalled_turns_is_drift() -> None:
    result = evaluate(_records(ok=40, stalled=16), BASELINE, target="shim")

    assert result.verdict is Verdict.DRIFT
    assert "stalled" in result.markdown


def test_a_small_rise_within_noise_passes() -> None:
    result = evaluate(_records(ok=51, stalled=6), BASELINE, target="shim")

    assert result.verdict is Verdict.PASS


def test_secondary_metrics_are_reported_but_never_gate() -> None:
    result = evaluate(_records(ok=20, stalled=4), BASELINE, target="shim")

    assert result.verdict is Verdict.PASS
    assert "success" in result.markdown


def test_many_upstream_errors_make_the_run_inconclusive() -> None:
    result = evaluate(_records(ok=30, stalled=10, errors=20), BASELINE, target="shim")

    assert result.verdict is Verdict.INCONCLUSIVE


def test_a_missing_target_is_inconclusive() -> None:
    result = evaluate(_records(ok=53, stalled=4), BASELINE, target="other")

    assert result.verdict is Verdict.INCONCLUSIVE


def test_zero_and_all_stalled_edges() -> None:
    assert evaluate(_records(ok=60, stalled=0), BASELINE, target="shim").verdict is Verdict.PASS
    assert evaluate(_records(ok=0, stalled=60), BASELINE, target="shim").verdict is Verdict.DRIFT


@pytest.mark.parametrize(("stalled", "code"), [(4, 0), (16, 1)], ids=["pass", "drift"])
def test_exit_codes(tmp_path: Path, stalled: int, code: int) -> None:
    results = tmp_path / "results.json"
    results.write_text(json.dumps(_records(ok=60 - stalled, stalled=stalled)))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(BASELINE))
    summary = tmp_path / "summary.md"

    exit_code = main(
        [str(results), "--baseline", str(baseline), "--target", "shim", "--summary", str(summary)]
    )

    assert exit_code == code
    assert summary.read_text().startswith("## Live eval")


def test_inconclusive_exit_code(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    results.write_text(json.dumps(_records(ok=10, stalled=0, errors=50)))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(BASELINE))

    assert main([str(results), "--baseline", str(baseline), "--target", "shim"]) == 3


def test_committed_baseline_is_well_formed() -> None:
    baseline = json.loads(Path("evals/baseline.json").read_text())

    assert baseline["source"]
    for name in ("stalled", "success", "progress"):
        metric = baseline["metrics"][name]
        assert 0 <= metric["k"] <= metric["n"] > 0


def test_a_small_clean_run_asks_for_more_repeats() -> None:
    result = evaluate(_records(ok=14, stalled=1, total=15), BASELINE, target="shim")

    assert result.verdict is Verdict.INCONCLUSIVE
    assert "more repeats" in result.markdown
    assert "Upstream errors" not in result.markdown
