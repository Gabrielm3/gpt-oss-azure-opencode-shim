"""Token usage, estimated cost and streaming latency for chat requests.

Azure reports ``usage`` in the last event of a stream even without
``stream_options.include_usage``, so the shim never changes the request to get
it. Answers that carry no usage are counted apart (``shim_usage_missing``), so
an undercount shows up instead of hiding.

Cost is an estimate: tokens times a list price. It ignores discounts, cached
input pricing and currency, so use it for trends and per-request comparisons,
and the Azure bill for money.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

# USD per 1M tokens (input, output), Global Standard, pay-as-you-go.
# Source: https://azure.microsoft.com/pricing/details/azure-openai/ and the
# Foundry model catalog, checked 2026-09-25. Override with SHIM_PRICES.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-oss-120b": (0.15, 0.60),
    "gpt-5-mini": (0.25, 2.00),
}


@dataclass(frozen=True)
class Usage:
    """Tokens of one answer. ``output_tokens`` includes reasoning tokens."""

    input_tokens: int
    output_tokens: int


def usage_from_summary(summary: Mapping[str, Any] | None) -> Usage | None:
    """Usage from ``telemetry.response_summary``, or ``None`` if not reported."""
    if not summary:
        return None
    tokens_in, tokens_out = summary.get("input_tokens"), summary.get("output_tokens")
    if not isinstance(tokens_in, int) or not isinstance(tokens_out, int):
        return None
    return Usage(tokens_in, tokens_out)


class PriceTable:
    """List prices per model, in USD per 1M tokens."""

    def __init__(self, prices: Mapping[str, tuple[float, float]]) -> None:
        self._prices = dict(prices)

    def cost(self, model: str, usage: Usage) -> float | None:
        """Estimated USD cost, or ``None`` for a model without a price."""
        price = self._prices.get(model)
        if price is None:
            return None
        per_input, per_output = price
        return (usage.input_tokens * per_input + usage.output_tokens * per_output) / 1_000_000

    @classmethod
    def from_env(cls, name: str = "SHIM_PRICES") -> PriceTable:
        """Defaults, overridden by ``model=input/output,...`` (USD per 1M tokens).

        Raises
        ------
        RuntimeError
            If the variable is not in that format.
        """
        prices = dict(DEFAULT_PRICES)
        raw = os.environ.get(name, "").strip()
        for part in filter(None, (p.strip() for p in raw.split(","))):
            try:
                model, pair = part.split("=", 1)
                per_input, per_output = (float(x) for x in pair.split("/", 1))
            except ValueError:
                raise RuntimeError(
                    f"{name} must look like 'model=0.15/0.60,other=1/2', got {part!r}"
                ) from None
            if per_input < 0 or per_output < 0:
                raise RuntimeError(f"{name} prices must not be negative, got {part!r}")
            prices[model.strip()] = (per_input, per_output)
        return cls(prices)


class SseMeter:
    """Watches a passthrough stream without buffering it.

    Records when the first generated token arrives (content, reasoning or a
    tool call delta, whichever comes first) and the usage in the final event.
    """

    def __init__(self, started: float, clock: Callable[[], float] = time.perf_counter) -> None:
        self.started = started
        self.first_token_at: float | None = None
        self.usage: Usage | None = None
        self._clock = clock
        self._pending = b""

    def feed(self, chunk: bytes) -> None:
        lines = (self._pending + chunk).split(b"\n")
        self._pending = lines.pop()
        for line in lines:
            self._line(line)

    def close(self) -> None:
        if self._pending:
            self._line(self._pending)
            self._pending = b""

    @property
    def time_to_first_token(self) -> float | None:
        return None if self.first_token_at is None else self.first_token_at - self.started

    def output_tokens_per_second(self, finished: float) -> float | None:
        """Output tokens over the generation time (first token to end of stream)."""
        if self.usage is None or self.first_token_at is None:
            return None
        elapsed = finished - self.first_token_at
        return self.usage.output_tokens / elapsed if elapsed > 0 else None

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if self.first_token_at is None and _has_token(event):
            self.first_token_at = self._clock()
        usage = event.get("usage")
        if isinstance(usage, dict):
            self.usage = usage_from_summary(
                {
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("completion_tokens"),
                }
            )


def _has_token(event: dict[str, Any]) -> bool:
    for choice in event.get("choices") or []:
        delta = choice.get("delta") or {}
        if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
            return True
    return False
