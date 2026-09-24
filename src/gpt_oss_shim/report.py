"""Summarize production outcomes from the shim's outcome log.

With ``SHIM_TRACE_DIR`` set, the shim appends one line per chat request to
``outcomes-YYYY-MM-DD.jsonl``: outcome, polyfill mode, whether the client
forced a tool call, HTTP status and latency, never content. This report turns
the last N days of those lines into rates with 95% Wilson intervals, so the
numbers survive restarts and do not need a Prometheus server.

Usage::

    gpt-oss-azure-opencode-shim-report ~/.local/share/gpt-oss-azure-opencode-shim/traces --days 7

Polyfill rates count forced requests answered with HTTP 200. Forced requests
with another status are upstream errors and are listed apart.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .stats import format_rate, percentile

_OUTCOME_FILE = re.compile(r"^outcomes-(\d{4}-\d{2}-\d{2})\.jsonl$")
_FORCED_OUTCOMES = ("native", "rescued", "failed", "empty_choices", "rewritten")
_STALLED = frozenset({"failed", "empty_choices"})


def load_outcomes(directory: Path, *, since: date) -> list[dict[str, Any]]:
    """Read every outcome line from daily files dated ``since`` or later."""
    records: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir()):
        match = _OUTCOME_FILE.match(path.name)
        if not match or date.fromisoformat(match.group(1)) < since:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def summarize_outcomes(records: list[dict[str, Any]]) -> str:
    """Render the outcome log as Markdown: traffic, polyfill rates, errors, latency."""
    forced = [r for r in records if r.get("forced")]
    answered = [r for r in forced if r.get("status") == 200]
    errors = Counter(r.get("status") for r in forced if r.get("status") != 200)
    outcomes = Counter(r["outcome"] for r in answered)
    stalled = sum(outcomes[name] for name in _STALLED)
    modes = Counter(r.get("mode") for r in forced)
    latencies = [r["seconds"] for r in answered]

    lines = [
        f"Chat requests: {len(records)}",
        f"Forced tool_choice: {format_rate(len(forced), len(records))}",
        f"Polyfill mode on forced requests: {_counts(modes)}",
        "",
        "| Forced outcome (HTTP 200) | Rate |",
        "| ------------------------- | ---- |",
    ]
    for name in _FORCED_OUTCOMES:
        if outcomes[name] or name != "rewritten":
            lines.append(f"| {name} | {format_rate(outcomes[name], len(answered))} |")
    lines += [
        "",
        f"Stalled turns (failed + empty_choices): {format_rate(stalled, len(answered))}",
        f"Forced requests without HTTP 200: {sum(errors.values())}"
        + (f" ({_counts(errors, sep=' × ')})" if errors else ""),
    ]
    if latencies:
        lines.append(
            f"Forced latency p50 / p95: {percentile(latencies, 50):.1f} / "
            f"{percentile(latencies, 95):.1f} s"
        )
    if outcomes["rewritten"]:
        lines.append(
            "Note: `rewritten` forced answers were not checked (polyfill `observe` or `off`); "
            "see `shim_polyfill_observed_total` or the traces for what a repair would do."
        )
    return "\n".join(lines)


def _counts(counter: Counter, sep: str = " ") -> str:
    return ", ".join(f"{k}{sep}{v}" for k, v in counter.most_common()) or "—"


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {raw!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gpt-oss-azure-opencode-shim-report", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("directory", type=Path, help="the shim's SHIM_TRACE_DIR")
    parser.add_argument(
        "--days", type=_positive_int, default=7, help="days to include, today counts"
    )
    args = parser.parse_args(argv)

    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=args.days - 1)
    records = load_outcomes(args.directory.expanduser(), since=since)
    if not records:
        print(f"no outcome records in {args.directory} since {since}", file=sys.stderr)
        return 1
    print(f"Window: {since} to {today} (UTC)\n")
    print(summarize_outcomes(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
