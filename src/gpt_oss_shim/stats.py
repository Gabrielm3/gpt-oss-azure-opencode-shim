"""Small-sample statistics for eval and production rates."""

from __future__ import annotations

import math
from collections.abc import Sequence


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Return the Wilson score interval for ``successes / total`` (95% by default).

    Unlike the normal approximation, it stays inside ``[0, 1]`` and still gives
    a useful upper bound at zero successes, which matters with 10 or 60 runs.
    """
    if total <= 0:
        raise ValueError("total must be positive")
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def format_rate(successes: int, total: int) -> str:
    """Render ``k/n (p%, 95% CI low–high%)``, or ``—`` when there is nothing to rate."""
    if total <= 0:
        return "—"
    low, high = wilson_interval(successes, total)
    return (
        f"{successes}/{total} ({100 * successes / total:.0f}%, "
        f"95% CI {100 * low:.0f}–{100 * high:.0f}%)"
    )


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile; ``None`` for an empty sequence."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def difference_interval(k1: int, n1: int, k2: int, n2: int, z: float = 1.96) -> tuple[float, float]:
    """Return the 95% interval for ``k1/n1 - k2/n2`` (Newcombe's hybrid score method).

    It combines the two Wilson intervals, so it inherits their behavior at 0 and
    n and accounts for the noise in both samples. When the interval excludes
    zero, the two rates differ beyond sampling noise.
    """
    p1, p2 = k1 / n1, k2 / n2
    low1, high1 = wilson_interval(k1, n1, z)
    low2, high2 = wilson_interval(k2, n2, z)
    difference = p1 - p2
    return (
        difference - math.sqrt((p1 - low1) ** 2 + (high2 - p2) ** 2),
        difference + math.sqrt((high1 - p1) ** 2 + (p2 - low2) ** 2),
    )
