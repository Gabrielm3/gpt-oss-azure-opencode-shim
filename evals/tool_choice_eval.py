"""Measure how often a forced tool_choice request gets the tool call it asked for.

Each scenario in ``evals/scenarios.py`` is sent to every target. A run
succeeds when the first tool call names the expected tool and its arguments
validate against that tool's JSON Schema. The report shows the success rate,
the ``x-shim-outcome`` values seen, and latency per target.

Usage::

    python -m evals.tool_choice_eval \\
        --target direct=$UPSTREAM_URL/v1/chat/completions \\
        --target shim=http://127.0.0.1:9526/v1/chat/completions \\
        --repeat 2 --out eval-results.json

A target named ``direct`` sends ``AZURE_FOUNDRY_API_KEY`` as the ``api-key``
header. Other targets are assumed to be a shim and get no credentials.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

import httpx
from jsonschema.validators import validator_for

from evals.scenarios import SCENARIOS

OUTCOME_HEADER = "x-shim-outcome"


def extract_tool_calls(body: bytes, *, stream: bool) -> list[tuple[str, str]] | None:
    """Return ``(name, arguments)`` for each tool call, or ``None`` if unparseable."""
    try:
        if not stream:
            message = json.loads(body)["choices"][0]["message"]
            return [
                (call["function"]["name"], call["function"].get("arguments") or "")
                for call in message.get("tool_calls") or []
            ]
        calls: dict[int, dict[str, str]] = {}
        for line in body.decode().splitlines():
            if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                continue
            event = json.loads(line[5:])
            for choice in event.get("choices") or []:
                for part in (choice.get("delta") or {}).get("tool_calls") or []:
                    call = calls.setdefault(part.get("index", 0), {"name": "", "arguments": ""})
                    function = part.get("function") or {}
                    call["name"] += function.get("name") or ""
                    call["arguments"] += function.get("arguments") or ""
        return [(c["name"], c["arguments"]) for _, c in sorted(calls.items())]
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def score(scenario: dict[str, Any], status: int, body: bytes, *, stream: bool) -> tuple[bool, str]:
    """Return ``(success, reason)`` for one response to ``scenario``."""
    if status != 200:
        return False, f"http_{status}"
    calls = extract_tool_calls(body, stream=stream)
    if calls is None:
        return False, "unparseable"
    if not calls:
        return False, "no_tool_call"

    name, arguments = calls[0]
    schemas = {t["function"]["name"]: t["function"].get("parameters") for t in scenario["tools"]}
    expected = scenario.get("expect_tool")
    if (expected is not None and name != expected) or name not in schemas:
        return False, f"wrong_tool:{name}"
    try:
        value = json.loads(arguments)
    except ValueError:
        return False, "invalid_arguments"
    schema = schemas[name] or {}
    if not validator_for(schema)(schema).is_valid(value):
        return False, "invalid_arguments"
    return True, "ok"


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile; ``None`` for an empty sequence."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def run_once(
    client: httpx.Client, target: str, url: str, scenario: dict[str, Any], model: str
) -> dict[str, Any]:
    stream = bool(scenario.get("stream"))
    payload = {
        "model": model,
        "messages": scenario["messages"],
        "tools": scenario["tools"],
        "tool_choice": scenario["tool_choice"],
        "stream": stream,
        "max_completion_tokens": 2048,
    }
    headers = {"content-type": "application/json"}
    if target == "direct":
        headers["api-key"] = os.environ["AZURE_FOUNDRY_API_KEY"]
    started = time.perf_counter()
    try:
        response = client.post(url, json=payload, headers=headers)
        status, body, outcome = (
            response.status_code,
            response.content,
            response.headers.get(OUTCOME_HEADER),
        )
    except httpx.HTTPError as exc:
        status, body, outcome = 0, b"", f"transport:{type(exc).__name__}"
    seconds = time.perf_counter() - started
    ok, reason = score(scenario, status, body, stream=stream) if status else (False, outcome)
    return {
        "target": target,
        "scenario": scenario["id"],
        "stream": stream,
        "ok": ok,
        "reason": reason,
        "outcome": outcome,
        "seconds": round(seconds, 3),
    }


# Reasons for which the client got no usable tool call at all: an agent loop stalls.
_STALLED = frozenset({"no_tool_call", "unparseable"})


def summarize(records: list[dict[str, Any]]) -> str:
    """Render a Markdown table: one row per target.

    ``Success`` is strict: the expected tool with schema-valid arguments.
    ``Stalled`` counts answers with no tool call at all, over the answers that
    came back with HTTP 200 (upstream errors are reported, not counted there).
    """
    lines = [
        "| Target | Success | Stalled turns | Outcomes (x-shim-outcome) | Failure reasons "
        "| p50 s | p95 s |",
        "| ------ | ------- | ------------- | ------------------------- | --------------- "
        "| ----- | ----- |",
    ]
    for target in dict.fromkeys(r["target"] for r in records):
        rows = [r for r in records if r["target"] == target]
        ok = sum(r["ok"] for r in rows)
        answered = [r for r in rows if not r["reason"].startswith(("http_", "transport:"))]
        stalled = sum(r["reason"] in _STALLED for r in answered)
        outcomes = Counter(r["outcome"] for r in rows if r["outcome"])
        reasons = Counter(r["reason"] for r in rows if not r["ok"])
        latencies = [r["seconds"] for r in rows]
        lines.append(
            f"| {target} | {_ratio(ok, len(rows))} | {_ratio(stalled, len(answered))} "
            f"| {_counts(outcomes)} | {_counts(reasons)} "
            f"| {percentile(latencies, 50):.1f} | {percentile(latencies, 95):.1f} |"
        )
    return "\n".join(lines)


def _ratio(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.0f}%)" if whole else "—"


def _counts(counter: Counter) -> str:
    return ", ".join(f"{k} {v}" for k, v in counter.most_common()) or "—"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", action="append", required=True, metavar="NAME=URL")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", default=os.environ.get("MODEL", "gpt-oss-120b"))
    parser.add_argument("--out", help="write every run as JSON to this file")
    args = parser.parse_args(argv)

    targets = [t.split("=", 1) for t in args.target]
    records = []
    with httpx.Client(timeout=httpx.Timeout(300, connect=10)) as client:
        for _ in range(args.repeat):
            for scenario in SCENARIOS:
                for name, url in targets:
                    record = run_once(client, name, url, scenario, args.model)
                    records.append(record)
                    mark = "ok  " if record["ok"] else "FAIL"
                    print(
                        f"{mark} {name:<10} {scenario['id']:<24} {record['reason']:<22} "
                        f"{record['outcome'] or '':<14} {record['seconds']:.1f}s",
                        file=sys.stderr,
                        flush=True,
                    )
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(records, fh, indent=2)
    print(summarize(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
