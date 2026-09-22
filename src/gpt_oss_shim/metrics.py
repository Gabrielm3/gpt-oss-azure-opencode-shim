"""Prometheus metrics built from request outcomes.

Each app gets its own registry, so several apps (tests, embedded use) never
share or double-register series. Every outcome label is created up front, so a
dashboard sees explicit zeros instead of missing series.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram
from prometheus_client.exposition import generate_latest

from .polyfill import Outcome

# Forced requests are buffered until the upstream finishes, so their duration
# is the latency the client sees before the first byte.
_FORCED_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600)


class Metrics:
    """Counters and histograms for one app instance."""

    content_type = CONTENT_TYPE_LATEST

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
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
        for outcome in Outcome:
            self.requests.labels(outcome.value)
        for outcome in (Outcome.NATIVE, Outcome.RESCUED, Outcome.FAILED, Outcome.EMPTY_CHOICES):
            self.forced_duration.labels(outcome.value)

    def record(self, outcome: Outcome, *, forced_seconds: float | None = None) -> None:
        """Count one request; ``forced_seconds`` is set for polyfilled requests."""
        self.requests.labels(outcome.value).inc()
        if forced_seconds is not None:
            self.forced_duration.labels(outcome.value).observe(forced_seconds)

    def render(self) -> bytes:
        """Return the Prometheus text exposition of this registry."""
        return generate_latest(self.registry)
