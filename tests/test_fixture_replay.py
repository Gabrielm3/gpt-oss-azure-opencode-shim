"""Replay real upstream answers through the polyfill.

Every file in ``tests/fixtures/polyfill/`` was promoted from a trace of a real
``gpt-oss-120b`` answer (see ``evals/fixtures.py``). A change to the polyfill
that alters the decision on any of them fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.fixtures import replay

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "polyfill").glob("*.json"))


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_polyfill_decision_on_recorded_answer(path: Path) -> None:
    fixture = json.loads(path.read_text())

    outcome, tool = replay(fixture)

    assert outcome == fixture["expected"]["outcome"]
    assert tool == fixture["expected"].get("tool")


def test_fixture_set_is_not_empty() -> None:
    assert FIXTURES, "promote at least one trace into tests/fixtures/polyfill/"
