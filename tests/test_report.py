"""Tests for the production report built from the shim's outcome log."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from evals.report import load_outcomes, main, summarize_outcomes


def _record(outcome: str, *, forced: bool = True, status: int = 200, **extra: Any) -> dict:
    return {
        "ts": "2026-09-20T10:00:00+00:00",
        "outcome": outcome,
        "mode": "on",
        "forced": forced,
        "stream": True,
        "status": status,
        "seconds": 2.0,
        "model": "gpt-oss-120b",
        **extra,
    }


def _write(directory: Path, day: str, records: list[dict]) -> None:
    lines = "".join(json.dumps(r) + "\n" for r in records)
    (directory / f"outcomes-{day}.jsonl").write_text(lines)


def test_load_outcomes_reads_only_the_window_and_only_outcome_files(tmp_path: Path) -> None:
    _write(tmp_path, "2026-09-10", [_record("native")])
    _write(tmp_path, "2026-09-20", [_record("rescued")])
    (tmp_path / "traces-2026-09-20.jsonl").write_text('{"tools": []}\n')

    records = load_outcomes(tmp_path, since=date(2026, 9, 14))

    assert [r["outcome"] for r in records] == ["rescued"]


def test_summary_rates_count_only_forced_answers_with_http_200() -> None:
    records = [
        *[_record("native") for _ in range(6)],
        _record("rescued"),
        _record("failed"),
        _record("empty_choices"),
        _record("rewritten", status=500),
        _record("passthrough", forced=False),
        _record("passthrough", forced=False),
    ]

    text = summarize_outcomes(records)

    assert "Chat requests: 12" in text
    assert "Forced tool_choice: 10/12 (83%" in text
    assert "| native | 6/9 (67%" in text
    assert "| rescued | 1/9 (11%" in text
    assert "Stalled turns (failed + empty_choices): 2/9 (22%" in text
    assert "Forced requests without HTTP 200: 1 (500 × 1)" in text


def test_summary_warns_when_the_polyfill_did_not_run() -> None:
    records = [_record("rewritten", mode="observe"), _record("native")]

    text = summarize_outcomes(records)

    assert "observe" in text
    assert "| rewritten | 1/2" in text


def test_empty_directory_says_so(tmp_path: Path, capsys) -> None:
    assert main([str(tmp_path), "--days", "7"]) == 1

    assert "no outcome records" in capsys.readouterr().err
