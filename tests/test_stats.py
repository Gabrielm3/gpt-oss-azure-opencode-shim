"""Tests for the confidence intervals printed by the eval and report tools."""

from __future__ import annotations

import pytest

from gpt_oss_shim.stats import format_rate, wilson_interval


def test_wilson_interval_matches_known_values() -> None:
    low, high = wilson_interval(49, 60)

    assert low == pytest.approx(0.7007, abs=1e-3)
    assert high == pytest.approx(0.8942, abs=1e-3)


def test_wilson_interval_stays_inside_zero_and_one() -> None:
    assert wilson_interval(0, 10)[0] == 0.0
    assert wilson_interval(10, 10)[1] == pytest.approx(1.0)
    assert wilson_interval(0, 10)[1] > 0, "zero successes still leave an upper bound"


def test_format_rate_shows_count_share_and_interval() -> None:
    assert format_rate(49, 60) == "49/60 (82%, 95% CI 70–89%)"
    assert format_rate(0, 0) == "—"


def test_difference_interval_matches_newcombe_worked_example() -> None:
    from gpt_oss_shim.stats import difference_interval

    # Newcombe (1998), method 10: 56/70 vs 48/80 -> 95% CI 0.0524 to 0.3339.
    low, high = difference_interval(56, 70, 48, 80)

    assert low == pytest.approx(0.0524, abs=1e-3)
    assert high == pytest.approx(0.3339, abs=1e-3)


def test_difference_interval_contains_zero_for_equal_rates() -> None:
    from gpt_oss_shim.stats import difference_interval

    low, high = difference_interval(4, 60, 4, 60)

    assert low < 0 < high
