"""Small-sample statistics for eval and production rates."""

from __future__ import annotations

import math


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
