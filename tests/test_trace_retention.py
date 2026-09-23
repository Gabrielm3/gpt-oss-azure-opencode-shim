"""Tests for trace retention and the per-request outcome log.

Traces grow with every forced request, so the writer deletes its own files
after ``retention_days``, keeps the directory under ``max_bytes``, and never
touches a file it did not create.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gpt_oss_shim.polyfill import Outcome, PolyfillMode
from gpt_oss_shim.traces import TraceWriter

REQUEST = {"model": "m", "tools": [], "tool_choice": "required"}


class Clock:
    def __init__(self, day: int) -> None:
        self.day = day

    def __call__(self) -> datetime:
        return datetime(2026, 9, self.day, 12, tzinfo=timezone.utc)


def _write(writer: TraceWriter, response: bytes = b"{}") -> None:
    writer.write(
        outcome=Outcome.FAILED,
        mode=PolyfillMode.ON,
        stream=False,
        request=REQUEST,
        response=response,
    )


def _log(writer: TraceWriter, **overrides: Any) -> None:
    fields: dict[str, Any] = {
        "outcome": Outcome.NATIVE,
        "mode": PolyfillMode.ON,
        "forced": True,
        "stream": False,
        "status": 200,
        "seconds": 1.234,
        "model": "gpt-oss-120b",
    }
    writer.log_outcome(**{**fields, **overrides})


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_files_older_than_retention_are_deleted(tmp_path: Path) -> None:
    clock = Clock(day=1)
    writer = TraceWriter(tmp_path, frozenset({Outcome.FAILED}), retention_days=3, now=clock)
    _write(writer)
    _log(writer)

    clock.day = 3
    _write(writer)
    clock.day = 4
    _write(writer)

    assert _names(tmp_path) == [
        "traces-2026-09-03.jsonl",
        "traces-2026-09-04.jsonl",
    ], "day 1 is older than 3 days on day 4, day 3 is not"


def test_files_it_does_not_own_are_never_deleted(tmp_path: Path) -> None:
    foreign = ["notes.txt", "traces-backup.jsonl", "traces-2026-09-01.jsonl.bak", "other.jsonl"]
    for name in foreign:
        (tmp_path / name).write_text("x" * 1000)
    clock = Clock(day=20)
    writer = TraceWriter(
        tmp_path, frozenset({Outcome.FAILED}), retention_days=1, max_bytes=10, now=clock
    )

    _write(writer)

    assert set(foreign) <= set(_names(tmp_path))


def test_oldest_days_are_deleted_to_stay_under_the_size_cap(tmp_path: Path) -> None:
    big = b"x" * 400
    clock = Clock(day=1)
    writer = TraceWriter(tmp_path, frozenset({Outcome.FAILED}), max_bytes=1500, now=clock)
    for day in (1, 2, 3):
        clock.day = day
        _write(writer, big)

    clock.day = 4
    _write(writer, big)
    _write(writer, big)

    names = _names(tmp_path)
    assert "traces-2026-09-01.jsonl" not in names
    assert "traces-2026-09-04.jsonl" in names
    assert sum((tmp_path / n).stat().st_size for n in names) <= 1500


def test_writes_that_would_pass_the_cap_are_dropped_and_reported(tmp_path: Path) -> None:
    dropped: list[int] = []
    writer = TraceWriter(
        tmp_path,
        frozenset({Outcome.FAILED}),
        max_bytes=1000,
        now=Clock(day=1),
        on_drop=lambda: dropped.append(1),
    )

    for _ in range(5):
        _write(writer, b"x" * 400)

    assert len(dropped) == 4, "one trace fits; today's file is never deleted, so the rest drop"
    assert (tmp_path / "traces-2026-09-01.jsonl").stat().st_size <= 1000


def test_existing_files_count_toward_the_cap_after_a_restart(tmp_path: Path) -> None:
    (tmp_path / "traces-2026-09-01.jsonl").write_text("x" * 900)
    dropped: list[int] = []
    writer = TraceWriter(
        tmp_path,
        frozenset({Outcome.FAILED}),
        max_bytes=1000,
        now=Clock(day=1),
        on_drop=lambda: dropped.append(1),
    )

    _write(writer, b"x" * 400)

    assert dropped == [1]


def test_outcome_log_records_every_request_without_content(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path, frozenset({Outcome.FAILED}), now=Clock(day=5))

    _log(writer)
    _log(writer, outcome=Outcome.PASSTHROUGH, forced=False, stream=True, seconds=0.2)

    lines = (tmp_path / "outcomes-2026-09-05.jsonl").read_text().splitlines()
    first, second = (json.loads(line) for line in lines)
    assert first == {
        "ts": "2026-09-05T12:00:00+00:00",
        "outcome": "native",
        "mode": "on",
        "forced": True,
        "stream": False,
        "status": 200,
        "seconds": 1.234,
        "model": "gpt-oss-120b",
    }
    assert second["outcome"] == "passthrough"
    assert second["forced"] is False


def test_outcome_log_is_kept_when_traces_hit_the_cap(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path, frozenset({Outcome.FAILED}), max_bytes=500, now=Clock(day=1))
    _write(writer, b"x" * 450)

    _log(writer)

    assert (tmp_path / "outcomes-2026-09-01.jsonl").exists()
