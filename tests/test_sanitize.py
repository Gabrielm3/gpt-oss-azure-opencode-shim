"""Tests for request-body sanitization."""

import json

from gpt_oss_shim.shim import sanitize_chat_body


def _dump(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_forced_function_choice_is_rewritten_to_auto() -> None:
    body = _dump({
        "model": "gpt-oss-120b",
        "messages": [{"role": "user", "content": "hi"}],
        "tool_choice": {"type": "function", "function": {"name": "glob"}},
    })
    out, notes = sanitize_chat_body(body)

    assert json.loads(out)["tool_choice"] == "auto"
    assert len(notes) == 1
    assert "tool_choice" in notes[0]


def test_required_string_is_rewritten_to_auto() -> None:
    body = _dump({"tool_choice": "required"})
    out, notes = sanitize_chat_body(body)

    assert json.loads(out)["tool_choice"] == "auto"
    assert len(notes) == 1


def test_auto_is_preserved() -> None:
    body = _dump({"tool_choice": "auto"})
    out, notes = sanitize_chat_body(body)

    assert json.loads(out)["tool_choice"] == "auto"
    assert notes == []


def test_none_is_preserved() -> None:
    body = _dump({"tool_choice": "none"})
    out, notes = sanitize_chat_body(body)

    assert json.loads(out)["tool_choice"] == "none"
    assert notes == []


def test_missing_tool_choice_is_untouched() -> None:
    body = _dump({"model": "m", "messages": []})
    out, notes = sanitize_chat_body(body)

    assert "tool_choice" not in json.loads(out)
    assert notes == []


def test_invalid_json_is_returned_unchanged() -> None:
    body = b"this is not json"
    out, notes = sanitize_chat_body(body)

    assert out == body
    assert notes == []


def test_empty_body_is_returned_unchanged() -> None:
    out, notes = sanitize_chat_body(b"")

    assert out == b""
    assert notes == []


def test_top_level_list_is_returned_unchanged() -> None:
    body = _dump([1, 2, 3])  # type: ignore[arg-type]
    out, notes = sanitize_chat_body(body)

    assert json.loads(out) == [1, 2, 3]
    assert notes == []
