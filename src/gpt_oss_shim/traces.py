"""Local traces of forced requests, the raw material for regression fixtures.

A trace records what the polyfill needs to replay a decision offline: the
candidate tools, the original ``tool_choice``, and the raw upstream answer
before any repair. It never records ``messages``, so prompts, file contents
and tool results stay out of the trace. The answer itself is model output and
can still contain user data, so traces live in a private local directory
(``0700``, files ``0600``) and must be reviewed before they become fixtures
(see ``evals/fixtures.py``).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .polyfill import Outcome, PolyfillMode

logger = logging.getLogger("gpt_oss_shim")

DEFAULT_TRACE_OUTCOMES = frozenset({Outcome.RESCUED, Outcome.FAILED, Outcome.EMPTY_CHOICES})


class TraceWriter:
    """Append one JSON line per traced forced request to a daily file."""

    def __init__(self, directory: Path, outcomes: frozenset[Outcome]) -> None:
        self.directory = directory
        self.outcomes = outcomes

    def write(
        self,
        *,
        outcome: Outcome,
        mode: PolyfillMode,
        stream: bool,
        request: Mapping[str, Any],
        response: bytes,
    ) -> None:
        """Record one decision; a write failure is logged, never raised."""
        if outcome not in self.outcomes:
            return
        now = datetime.now(timezone.utc)
        record = {
            "ts": now.isoformat(timespec="seconds"),
            "outcome": outcome.value,
            "mode": mode.value,
            "stream": stream,
            "model": request.get("model"),
            "tool_choice": request.get("tool_choice"),
            "tools": request.get("tools") or [],
            "response": response.decode("utf-8", errors="replace"),
        }
        path = self.directory / f"traces-{now:%Y-%m-%d}.jsonl"
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("could not write a polyfill trace to %s", self.directory)
