"""Golden arguments: did the tool call carry the right values, not just valid ones?

Schema validation proves the arguments have the right shape. A scenario's
``expect_args`` says what the values must be, for the fields that have exactly
one right answer (an extracted age, a file list, an enum the prompt pins
down). Fields a reasonable model could fill differently, such as a triage
severity or a free-text summary, are not scored.

Each expectation is ``field: (matcher, expected)``:

- ``exact``: equal after JSON decoding (numbers compare by value).
- ``text``: equal after trimming and case folding.
- ``oneof``: ``text`` equal to any of the expected strings.
- ``files``: the same set of file names, ignoring directories and order.
- ``names``: a list of objects whose ``name`` fields form the expected set.

This metric is reported, never gated: the gate stays on one metric (see
``evals/gate.py``).
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Callable, Iterable
from typing import Any


def _text(value: Any) -> str | None:
    return value.strip().casefold() if isinstance(value, str) else None


def _exact(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return float(actual) == float(expected)
    return bool(actual == expected)


def _match_text(actual: Any, expected: str) -> bool:
    return _text(actual) == _text(expected)


def _oneof(actual: Any, expected: Iterable[str]) -> bool:
    return _text(actual) in {_text(e) for e in expected}


def _file_set(values: Any) -> set[str] | None:
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return None
    return {posixpath.basename(v.strip()).casefold() for v in values}


def _files(actual: Any, expected: Iterable[str]) -> bool:
    return _file_set(actual) == {e.casefold() for e in expected}


def _names(actual: Any, expected: Iterable[str]) -> bool:
    if not isinstance(actual, list) or not all(isinstance(v, dict) for v in actual):
        return False
    names = {_text(item.get("name")) for item in actual}
    return names == {_text(e) for e in expected}


MATCHERS: dict[str, Callable[[Any, Any], bool]] = {
    "exact": _exact,
    "text": _match_text,
    "oneof": _oneof,
    "files": _files,
    "names": _names,
}


def args_correct(expect_args: dict[str, tuple[str, Any]] | None, arguments: str) -> bool | None:
    """``True``/``False`` when the scenario has golden arguments, else ``None``.

    Every expected field must match; a missing field is a mismatch.
    """
    if not expect_args:
        return None
    try:
        value = json.loads(arguments)
    except ValueError:
        return False
    if not isinstance(value, dict):
        return False
    return all(
        field in value and MATCHERS[matcher](value[field], expected)
        for field, (matcher, expected) in expect_args.items()
    )
