"""Decide whether a live eval run shows upstream drift against a baseline.

The nightly live eval runs unchanged code from ``master`` against Azure, so a
change in its rates means the upstream model changed, not the shim. This gate
compares one run with a committed baseline and returns one of three verdicts:

``pass`` (exit 0)
    The primary metric is within sampling noise of the baseline.
``drift`` (exit 1)
    Stalled turns (answers with no tool call, the failure a user sees) rose
    beyond noise: the 95% Newcombe interval for ``run - baseline`` lies
    entirely above zero.
``inconclusive`` (exit 3)
    Too few answers to judge: most requests hit upstream errors, or the target
    is missing from the results. Treated as an infrastructure problem.

Only one metric gates. Strict success and progress are reported with their
intervals but never fail the run: checking three metrics at 95% every night
would raise false alarms on a regular schedule.

Usage::

    python -m evals.gate results.json --baseline evals/baseline.json \\
        --target shim --summary summary.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from evals.tool_choice_eval import STALLED_REASONS
from gpt_oss_shim.stats import difference_interval, format_rate

# Below this share of answered requests, upstream errors dominate the run.
MIN_ANSWERED_SHARE = 0.8
MIN_ANSWERED = 30


class Verdict(Enum):
    PASS = 0
    DRIFT = 1
    INCONCLUSIVE = 3


@dataclass(frozen=True)
class GateResult:
    verdict: Verdict
    markdown: str


def evaluate(records: list[dict[str, Any]], baseline: dict[str, Any], *, target: str) -> GateResult:
    """Compare the ``target`` rows of one eval run with the baseline counts."""
    rows = [r for r in records if r["target"] == target]
    answered = [r for r in rows if not r["reason"].startswith(("http_", "transport:"))]
    lines = [f"## Live eval: `{target}`", ""]

    if not rows:
        lines.append(f"**Inconclusive**: no results for target `{target}`. Broken setup.")
        return GateResult(Verdict.INCONCLUSIVE, "\n".join(lines))
    if len(answered) < MIN_ANSWERED_SHARE * len(rows):
        lines.append(
            f"**Inconclusive**: only {len(answered)} of {len(rows)} requests got an answer "
            f"(need {MIN_ANSWERED_SHARE:.0%}). Upstream errors, not a model change."
        )
        return GateResult(Verdict.INCONCLUSIVE, "\n".join(lines))
    if len(answered) < MIN_ANSWERED:
        lines.append(
            f"**Inconclusive**: {len(answered)} answers are too few to compare "
            f"(need at least {MIN_ANSWERED}). Run more repeats."
        )
        return GateResult(Verdict.INCONCLUSIVE, "\n".join(lines))

    current = {
        "stalled": (sum(r["reason"] in STALLED_REASONS for r in answered), len(answered)),
        "success": (sum(r["ok"] for r in rows), len(rows)),
        "progress": (sum(r.get("progress", r["ok"]) for r in rows), len(rows)),
    }
    lines += [
        "| Metric | Tonight | Baseline | Difference (95% CI) | Gates |",
        "| ------ | ------- | -------- | ------------------- | ----- |",
    ]
    drift = False
    for name, (k, n) in current.items():
        base = baseline["metrics"][name]
        low, high = difference_interval(k, n, base["k"], base["n"])
        gates = name == "stalled"
        if gates and low > 0:
            drift = True
        lines.append(
            f"| {name} | {format_rate(k, n)} | {format_rate(base['k'], base['n'])} "
            f"| {100 * low:+.0f} to {100 * high:+.0f} pts | {'yes' if gates else 'no'} |"
        )

    lines.append("")
    if drift:
        lines.append(
            "**Drift**: stalled turns rose beyond sampling noise. The code on `master` did "
            "not change, so the upstream model behaves differently."
        )
    else:
        lines.append("**Pass**: stalled turns are within sampling noise of the baseline.")
    lines.append(f"\nBaseline: {baseline['source']}")
    return GateResult(Verdict.DRIFT if drift else Verdict.PASS, "\n".join(lines))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", type=Path, help="JSON written by tool_choice_eval --out")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--summary", type=Path, help="also write the Markdown report here")
    args = parser.parse_args(argv)

    result = evaluate(
        json.loads(args.results.read_text()),
        json.loads(args.baseline.read_text()),
        target=args.target,
    )
    print(result.markdown)
    if args.summary:
        args.summary.write_text(result.markdown + "\n")
    return result.verdict.value


if __name__ == "__main__":
    sys.exit(main())
