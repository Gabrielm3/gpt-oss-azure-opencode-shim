"""Prometheus metrics built from request outcomes.

Each app gets its own registry, so several apps (tests, embedded use) never
share or double-register series. Every outcome label is created up front, so a
dashboard sees explicit zeros instead of missing series.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram
from prometheus_client.exposition import generate_latest

from .polyfill import Outcome
from .usage import PriceTable, Usage

# Forced requests are buffered until the upstream finishes, so their duration
# is the latency the client sees before the first byte.
_FORCED_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600)
_POLYFILL_OUTCOMES = (Outcome.NATIVE, Outcome.RESCUED, Outcome.FAILED, Outcome.EMPTY_CHOICES)
_TTFT_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 20, 30, 60)
_TOKENS_PER_SECOND_BUCKETS = (5, 10, 20, 40, 60, 80, 100, 150, 200, 300, 500)

# Clients choose the model string, so the label is capped: models past this
# many distinct values are counted as "other".
MAX_MODEL_LABELS = 16
OTHER_MODEL = "other"


class Metrics:
    """Counters and histograms for one app instance."""

    content_type = CONTENT_TYPE_LATEST

    def __init__(self, prices: PriceTable | None = None) -> None:
        self.registry = CollectorRegistry()
        self._prices = prices or PriceTable({})
        self._models: set[str] = set()
        self.requests = Counter(
            "shim_requests",
            "Forwarded requests by outcome.",
            ["outcome"],
            registry=self.registry,
        )
        self.forced_duration = Histogram(
            "shim_forced_request_duration_seconds",
            "Time to answer a forced tool_choice request, including buffering.",
            ["outcome"],
            buckets=_FORCED_BUCKETS,
            registry=self.registry,
        )
        self.observed = Counter(
            "shim_polyfill_observed",
            "Forced requests in observe mode, by the outcome a repair would have had.",
            ["outcome"],
            registry=self.registry,
        )
        self.traces_dropped = Counter(
            "shim_traces_dropped",
            "Traces not written because the trace directory reached its size cap.",
            registry=self.registry,
        )
        self.tokens = Counter(
            "shim_tokens",
            "Tokens reported by the upstream, by model and type (input or output; "
            "output includes reasoning).",
            ["model", "type"],
            registry=self.registry,
        )
        self.cost = Counter(
            "shim_cost_usd",
            "Estimated cost in USD at list price (SHIM_PRICES); models without a price add 0.",
            ["model"],
            registry=self.registry,
        )
        self.usage_missing = Counter(
            "shim_usage_missing",
            "Chat answers with HTTP 200 that reported no token usage.",
            ["model"],
            registry=self.registry,
        )
        self.time_to_first_token = Histogram(
            "shim_time_to_first_token_seconds",
            "Passthrough streams: request start to the first generated token "
            "(reasoning, content or tool call). Buffered forced requests are excluded.",
            ["model"],
            buckets=_TTFT_BUCKETS,
            registry=self.registry,
        )
        self.output_tokens_per_second = Histogram(
            "shim_output_tokens_per_second",
            "Passthrough streams: output tokens over the time from first token to end.",
            ["model"],
            buckets=_TOKENS_PER_SECOND_BUCKETS,
            registry=self.registry,
        )
        for outcome in Outcome:
            self.requests.labels(outcome.value)
        for outcome in _POLYFILL_OUTCOMES:
            self.forced_duration.labels(outcome.value)
            self.observed.labels(outcome.value)

    def record(self, outcome: Outcome, *, forced_seconds: float | None = None) -> None:
        """Count one request; ``forced_seconds`` is set for polyfilled requests."""
        self.requests.labels(outcome.value).inc()
        if forced_seconds is not None:
            self.forced_duration.labels(outcome.value).observe(forced_seconds)

    def record_observed(self, outcome: Outcome) -> None:
        """Count what the polyfill would have done to an answer it did not change."""
        self.observed.labels(outcome.value).inc()

    def model_label(self, model: object) -> str:
        """A bounded label value for a client-supplied model name."""
        name = model if isinstance(model, str) and model else "unknown"
        if name in self._models:
            return name
        if len(self._models) < MAX_MODEL_LABELS:
            self._models.add(name)
            return name
        return OTHER_MODEL

    def record_usage(self, model: object, usage: Usage | None) -> None:
        """Count the tokens and estimated cost of one answer with HTTP 200."""
        label = self.model_label(model)
        if usage is None:
            self.usage_missing.labels(label).inc()
            return
        self.tokens.labels(label, "input").inc(usage.input_tokens)
        self.tokens.labels(label, "output").inc(usage.output_tokens)
        cost = self._prices.cost(model, usage) if isinstance(model, str) else None
        self.cost.labels(label).inc(cost or 0.0)

    def record_stream_timing(
        self, model: object, *, ttft: float | None, tokens_per_second: float | None
    ) -> None:
        """Record the latency of one passthrough stream."""
        label = self.model_label(model)
        if ttft is not None:
            self.time_to_first_token.labels(label).observe(ttft)
        if tokens_per_second is not None:
            self.output_tokens_per_second.labels(label).observe(tokens_per_second)

    def record_trace_dropped(self) -> None:
        """Count one trace lost to the size cap."""
        self.traces_dropped.inc()

    def render(self) -> bytes:
        """Return the Prometheus text exposition of this registry."""
        return generate_latest(self.registry)
