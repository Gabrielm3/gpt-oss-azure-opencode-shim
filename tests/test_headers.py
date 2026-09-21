"""Tests for upstream header construction."""

from gpt_oss_shim.shim import build_upstream_headers


def test_headers_include_api_key_and_bearer() -> None:
    headers = build_upstream_headers({"api_key": "test-key"})

    assert headers["api-key"] == "test-key"
    assert headers["Authorization"] == "Bearer test-key"


def test_headers_force_json_content_type() -> None:
    headers = build_upstream_headers({"api_key": "k"})

    assert headers["Content-Type"] == "application/json"


def test_headers_disable_compression_for_sse() -> None:
    headers = build_upstream_headers({"api_key": "k"})

    assert headers["Accept-Encoding"] == "identity"
