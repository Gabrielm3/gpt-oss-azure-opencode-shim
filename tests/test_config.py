"""Tests for environment-based configuration."""

from __future__ import annotations

import pytest

from gpt_oss_shim.polyfill import Outcome, PolyfillMode
from gpt_oss_shim.shim import _load_config

REQUIRED = {
    "UPSTREAM_URL": "https://upstream.test/openai/",
    "AZURE_FOUNDRY_API_KEY": "k",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (
        "UPSTREAM_URL",
        "AZURE_FOUNDRY_API_KEY",
        "SHIM_HOST",
        "SHIM_PORT",
        "SHIM_ALLOWED_HOSTS",
        "SHIM_CONNECT_TIMEOUT",
        "SHIM_READ_TIMEOUT",
        "SHIM_TOOL_POLYFILL",
        "SHIM_TRACE_DIR",
        "SHIM_TRACE_OUTCOMES",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_missing_upstream_url_raises(env: pytest.MonkeyPatch) -> None:
    env.delenv("UPSTREAM_URL")

    with pytest.raises(RuntimeError, match="UPSTREAM_URL"):
        _load_config()


def test_missing_api_key_means_entra_auth(env: pytest.MonkeyPatch) -> None:
    env.delenv("AZURE_FOUNDRY_API_KEY")

    assert _load_config()["api_key"] is None


def test_upstream_trailing_slash_is_stripped(env: pytest.MonkeyPatch) -> None:
    assert _load_config()["upstream"] == "https://upstream.test/openai"


def test_allowed_hosts_default_to_loopback(env: pytest.MonkeyPatch) -> None:
    assert _load_config()["allowed_hosts"] == frozenset({"localhost", "127.0.0.1", "::1"})


def test_allowed_hosts_are_extended_from_env(env: pytest.MonkeyPatch) -> None:
    env.setenv("SHIM_ALLOWED_HOSTS", " Shim.Internal , 10.0.0.5 ,")

    allowed = _load_config()["allowed_hosts"]

    assert {"shim.internal", "10.0.0.5", "localhost"} <= allowed
    assert "" not in allowed


def test_timeouts_have_defaults(env: pytest.MonkeyPatch) -> None:
    config = _load_config()

    assert config["connect_timeout"] == 10.0
    assert config["read_timeout"] == 600.0


def test_timeouts_are_read_from_env(env: pytest.MonkeyPatch) -> None:
    env.setenv("SHIM_CONNECT_TIMEOUT", "2.5")
    env.setenv("SHIM_READ_TIMEOUT", "30")

    config = _load_config()

    assert config["connect_timeout"] == 2.5
    assert config["read_timeout"] == 30.0


@pytest.mark.parametrize("value", ["abc", "0", "-1"])
def test_invalid_timeout_raises(env: pytest.MonkeyPatch, value: str) -> None:
    env.setenv("SHIM_READ_TIMEOUT", value)

    with pytest.raises(RuntimeError, match="SHIM_READ_TIMEOUT"):
        _load_config()


def test_invalid_port_raises(env: pytest.MonkeyPatch) -> None:
    env.setenv("SHIM_PORT", "abc")

    with pytest.raises(RuntimeError, match="SHIM_PORT"):
        _load_config()


def test_tool_polyfill_is_on_by_default(env: pytest.MonkeyPatch) -> None:
    assert _load_config()["tool_polyfill"] is PolyfillMode.ON


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("off", PolyfillMode.OFF),
        ("0", PolyfillMode.OFF),
        ("ON", PolyfillMode.ON),
        ("true", PolyfillMode.ON),
        ("Observe", PolyfillMode.OBSERVE),
    ],
)
def test_tool_polyfill_mode_is_read_from_env(
    env: pytest.MonkeyPatch, value: str, expected: PolyfillMode
) -> None:
    env.setenv("SHIM_TOOL_POLYFILL", value)

    assert _load_config()["tool_polyfill"] is expected


def test_invalid_tool_polyfill_flag_raises(env: pytest.MonkeyPatch) -> None:
    env.setenv("SHIM_TOOL_POLYFILL", "maybe")

    with pytest.raises(RuntimeError, match="SHIM_TOOL_POLYFILL"):
        _load_config()


def test_tracing_is_off_by_default(env: pytest.MonkeyPatch) -> None:
    config = _load_config()

    assert config["trace_dir"] is None
    assert config["trace_outcomes"] == frozenset(
        {Outcome.RESCUED, Outcome.FAILED, Outcome.EMPTY_CHOICES}
    )


def test_trace_settings_are_read_from_env(env: pytest.MonkeyPatch, tmp_path) -> None:
    env.setenv("SHIM_TRACE_DIR", str(tmp_path))
    env.setenv("SHIM_TRACE_OUTCOMES", "failed, native")

    config = _load_config()

    assert config["trace_dir"] == tmp_path
    assert config["trace_outcomes"] == frozenset({Outcome.FAILED, Outcome.NATIVE})


def test_unknown_trace_outcome_raises(env: pytest.MonkeyPatch) -> None:
    env.setenv("SHIM_TRACE_OUTCOMES", "failed,sometimes")

    with pytest.raises(RuntimeError, match="SHIM_TRACE_OUTCOMES"):
        _load_config()


def test_trace_retention_defaults(env: pytest.MonkeyPatch) -> None:
    config = _load_config()

    assert config["trace_max_bytes"] == 100 * 1024 * 1024
    assert config["trace_retention_days"] == 14


def test_trace_retention_is_read_from_env(env: pytest.MonkeyPatch) -> None:
    env.setenv("SHIM_TRACE_MAX_MB", "2.5")
    env.setenv("SHIM_TRACE_RETENTION_DAYS", "3")

    config = _load_config()

    assert config["trace_max_bytes"] == int(2.5 * 1024 * 1024)
    assert config["trace_retention_days"] == 3


def test_fractional_retention_days_raise(env: pytest.MonkeyPatch) -> None:
    env.setenv("SHIM_TRACE_RETENTION_DAYS", "1.5")

    with pytest.raises(RuntimeError, match="SHIM_TRACE_RETENTION_DAYS"):
        _load_config()
