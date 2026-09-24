"""Local traces of forced requests, the raw material for regression fixtures.

A trace records what the polyfill needs to replay a decision offline: the
candidate tools, the original ``tool_choice``, and the raw upstream answer
before any repair. It never records ``messages``, so prompts, file contents
and tool results stay out of the trace. The answer itself is model output and
can still contain user data, so traces live in a private local directory
(``0700``, files ``0600``) and must be reviewed before they become fixtures
(see ``evals/fixtures.py``).

Next to the traces, an outcome log gets one small line per chat request
(outcome, status, latency, no content), so production rates can be measured
over days with ``gpt-oss-azure-opencode-shim-report`` even across restarts.

Both kinds of file are daily and bounded: files older than
``retention_days`` are deleted, and the oldest days of traces go first when
the directory passes ``max_bytes``. Outcome logs leave only through retention.
When today's traces alone fill the cap, new traces are dropped (``on_drop``)
while the tiny outcome lines are still kept.
Only files named ``traces-YYYY-MM-DD.jsonl`` or ``outcomes-YYYY-MM-DD.jsonl``
are ever deleted.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .polyfill import Outcome, PolyfillMode

logger = logging.getLogger("gpt_oss_shim")

DEFAULT_TRACE_OUTCOMES = frozenset({Outcome.RESCUED, Outcome.FAILED, Outcome.EMPTY_CHOICES})
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 14

_OWNED_FILE = re.compile(r"^(?:traces|outcomes)-(\d{4}-\d{2}-\d{2})\.jsonl$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TraceWriter:
    """Append traces and outcome lines to daily files under a size and age limit."""

    def __init__(
        self,
        directory: Path,
        outcomes: frozenset[Outcome],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        now: Callable[[], datetime] = _utc_now,
        on_drop: Callable[[], None] | None = None,
    ) -> None:
        self.directory = directory
        self.outcomes = outcomes
        self.max_bytes = max_bytes
        self.retention_days = retention_days
        self._now = now
        self._on_drop = on_drop
        self._files: dict[Path, tuple[date, int]] = {}
        self._pruned_on: date | None = None
        self._warned_on: date | None = None

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
        now = self._now()
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
        self._append("traces", now, record, capped=True)

    def log_outcome(
        self,
        *,
        outcome: Outcome,
        mode: PolyfillMode,
        forced: bool,
        stream: bool,
        status: int,
        seconds: float,
        model: Any,
    ) -> None:
        """Record what happened to one chat request, without any content."""
        now = self._now()
        record = {
            "ts": now.isoformat(timespec="seconds"),
            "outcome": outcome.value,
            "mode": mode.value,
            "forced": forced,
            "stream": stream,
            "status": status,
            "seconds": round(seconds, 3),
            "model": model,
        }
        self._append("outcomes", now, record, capped=False)

    def _append(self, kind: str, now: datetime, record: dict[str, Any], *, capped: bool) -> None:
        today = now.date()
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        path = self.directory / f"{kind}-{today:%Y-%m-%d}.jsonl"
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._pruned_on != today:
                self._prune(today)
            if capped and not self._make_room(len(line), today):
                self._drop(today)
                return
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "ab") as fh:
                fh.write(line)
            day, size = self._files.get(path, (today, 0))
            self._files[path] = (day, size + len(line))
        except OSError:
            logger.exception("could not write a %s record to %s", kind, self.directory)

    def _prune(self, today: date) -> None:
        """Rescan owned files and delete those past the retention window."""
        self._files = {}
        oldest_kept = today - timedelta(days=self.retention_days - 1)
        for path in self.directory.iterdir():
            match = _OWNED_FILE.match(path.name)
            if not match or not path.is_file():
                continue
            try:
                day = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if day < oldest_kept:
                path.unlink(missing_ok=True)
                logger.info("deleted expired trace file %s", path.name)
            else:
                self._files[path] = (day, path.stat().st_size)
        self._pruned_on = today

    def _make_room(self, needed: int, today: date) -> bool:
        """Delete the oldest past days of traces until ``needed`` bytes fit under the cap."""
        while sum(size for _, size in self._files.values()) + needed > self.max_bytes:
            past = [
                (day, path)
                for path, (day, _) in self._files.items()
                if day < today and path.name.startswith("traces-")
            ]
            if not past:
                return False
            _, oldest = min(past)
            oldest.unlink(missing_ok=True)
            del self._files[oldest]
            logger.info("deleted trace file %s to stay under the size cap", oldest.name)
        return True

    def _drop(self, today: date) -> None:
        if self._on_drop is not None:
            self._on_drop()
        if self._warned_on != today:
            self._warned_on = today
            logger.warning(
                "trace directory %s reached %d bytes; dropping new traces until tomorrow",
                self.directory,
                self.max_bytes,
            )
