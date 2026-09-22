"""Turn polyfill traces into regression fixtures.

The shim writes traces when ``SHIM_TRACE_DIR`` is set (see
``src/gpt_oss_shim/traces.py``). A trace holds a real upstream answer, so
promoting one to ``tests/fixtures/polyfill/`` makes the polyfill's decision on
that answer part of the test suite, replayed offline on every CI run.

Review each trace before promoting it: the answer is model output and can
contain user data. ``--keep-tools`` drops tools that reveal local setup, and
``--replace OLD=NEW`` scrubs strings such as absolute paths. Streamed text is
split across events, so a replacement that still matches the joined text is
an error; ``--coalesce`` merges the text deltas first so the replacement applies.

Usage::

    python -m evals.fixtures list TRACES.jsonl
    python -m evals.fixtures promote TRACES.jsonl --line 3 --id opencode-json-text \\
        --description "OpenCode structured output answered as JSON text" \\
        --keep-tools glob,read,StructuredOutput --replace /home/me/project=/repo --coalesce
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from evals.tool_choice_eval import extract_tool_calls
from gpt_oss_shim.polyfill import forced_tool_choice, repair_completion, repair_stream

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "polyfill"


def load_traces(path: Path) -> list[dict[str, Any]]:
    """Read a trace file: one JSON object per line."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def replay(fixture: dict[str, Any]) -> tuple[str, str | None]:
    """Run the polyfill on a recorded answer: ``(outcome, repaired tool name)``."""
    forced = forced_tool_choice({"tool_choice": fixture["tool_choice"], "tools": fixture["tools"]})
    if forced is None:
        raise ValueError("the recorded request does not force a tool call")
    repair = repair_stream if fixture["stream"] else repair_completion
    repaired, outcome = repair(fixture["response"].encode(), forced)
    calls = extract_tool_calls(repaired, stream=fixture["stream"]) or []
    return outcome.value, (calls[0][0] if calls else None)


_TEXT_FIELDS = ("content", "reasoning_content", "reasoning")


def _sse_events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data:") :])
        for line in body.splitlines()
        if line.startswith("data:") and line[len("data:") :].strip() != "[DONE]"
    ]


def stream_text(body: str) -> str:
    """All streamed text (content and reasoning) joined, as a reader would see it."""
    return "".join(
        (choice.get("delta") or {}).get(field) or ""
        for event in _sse_events(body)
        for choice in event.get("choices") or []
        for field in _TEXT_FIELDS
    )


def coalesce_stream(body: str) -> str:
    """Merge the text deltas of a stream into one event, keeping everything else.

    Streamed text arrives a few characters per event, so a string to scrub is
    usually split across events. Merging the text first makes replacements
    reliable. Tool-call deltas, finish reasons and usage keep their events;
    the polyfill joins text deltas anyway, so its decision does not change.
    """
    events = _sse_events(body)
    merged = dict.fromkeys(_TEXT_FIELDS, "")
    rest = []
    for event in events:
        choices = []
        for choice in event.get("choices") or []:
            delta = dict(choice.get("delta") or {})
            for field in _TEXT_FIELDS:
                merged[field] += delta.pop(field, None) or ""
            delta.pop("role", None)
            if delta or choice.get("finish_reason"):
                choices.append({**choice, "delta": delta})
        if choices or event.get("usage") or "error" in event:
            rest.append({**event, "choices": choices})
    head = {k: events[0][k] for k in ("id", "object", "created", "model") if k in events[0]}
    text = {field: value for field, value in merged.items() if value}
    first = {
        **head,
        "choices": [{"index": 0, "delta": {"role": "assistant", **text}, "finish_reason": None}],
    }
    lines = [f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in [first, *rest]]
    return "".join(lines) + "data: [DONE]\n\n"


def build_fixture(
    trace: dict[str, Any],
    *,
    fixture_id: str,
    description: str,
    keep_tools: Iterable[str] | None = None,
    replacements: Iterable[tuple[str, str]] = (),
    coalesce: bool = False,
) -> dict[str, Any]:
    """Build a fixture from one trace, with its expected outcome computed now.

    Raises ``ValueError`` if a string to replace survives in the streamed text,
    which happens when it is split across events and ``coalesce`` is off.
    """
    replacements = list(replacements)
    tools = trace["tools"]
    if keep_tools is not None:
        wanted = list(keep_tools)
        present = {t.get("function", {}).get("name") for t in tools}
        missing = [name for name in wanted if name not in present]
        if missing:
            raise ValueError(f"tools not in the trace: {missing}")
        tools = [t for t in tools if t.get("function", {}).get("name") in wanted]

    response = trace["response"]
    if coalesce and trace["stream"]:
        response = coalesce_stream(response)
    tools_text = json.dumps(tools)
    for old, new in replacements:
        response = response.replace(old, new)
        tools_text = tools_text.replace(old, new)
    if trace["stream"]:
        survivors = [old for old, _ in replacements if old in stream_text(response)]
        if survivors:
            raise ValueError(
                f"{survivors} still appear in the streamed text, split across events; "
                "promote again with --coalesce"
            )

    fixture = {
        "id": fixture_id,
        "description": description,
        "source": {"model": trace.get("model"), "ts": trace.get("ts"), "mode": trace.get("mode")},
        "stream": bool(trace["stream"]),
        "tool_choice": trace["tool_choice"],
        "tools": json.loads(tools_text),
        "response": response,
    }
    outcome, tool = replay(fixture)
    fixture["expected"] = {"outcome": outcome, **({"tool": tool} if tool else {})}
    return fixture


def _summary(trace: dict[str, Any]) -> str:
    names = ",".join(t.get("function", {}).get("name", "?") for t in trace.get("tools", []))
    kind = "stream" if trace.get("stream") else "json"
    return f"{trace.get('ts')} {trace.get('outcome'):<14} {kind:<6} tools={names}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="show the traces in a file")
    listing.add_argument("traces", type=Path)

    promote = sub.add_parser("promote", help="write one trace as a fixture")
    promote.add_argument("traces", type=Path)
    promote.add_argument("--line", type=int, required=True, help="1-based line in the file")
    promote.add_argument("--id", required=True, dest="fixture_id")
    promote.add_argument("--description", required=True)
    promote.add_argument("--keep-tools", help="comma-separated tool names to keep")
    promote.add_argument("--replace", action="append", default=[], metavar="OLD=NEW")
    promote.add_argument(
        "--coalesce",
        action="store_true",
        help="merge streamed text deltas so --replace reaches strings split across events",
    )
    promote.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    promote.add_argument("--force", action="store_true", help="overwrite an existing fixture")
    args = parser.parse_args(argv)

    traces = load_traces(args.traces)
    if args.command == "list":
        for number, trace in enumerate(traces, start=1):
            print(f"{number:>4}  {_summary(trace)}")
        return 0

    target = args.out_dir / f"{args.fixture_id}.json"
    if target.exists() and not args.force:
        print(f"{target} exists; pass --force to overwrite", file=sys.stderr)
        return 1
    fixture = build_fixture(
        traces[args.line - 1],
        fixture_id=args.fixture_id,
        description=args.description,
        keep_tools=args.keep_tools.split(",") if args.keep_tools else None,
        replacements=[tuple(pair.split("=", 1)) for pair in args.replace],
        coalesce=args.coalesce,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {target}: expected {fixture['expected']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
