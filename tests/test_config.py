"""Tests for environment-based configuration."""

from __future__ import annotations

import pytest

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
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_missing_upstream_url_raises(env: pytest.MonkeyPatch) -> None:
    env.delenv("UPSTREAM_URL")

    with pytest.raises(RuntimeError, match="UPSTREAM_URL"):
        _load_config()


def test_missing_api_key_raises(env: pytest.MonkeyPatch) -> None:
    env.delenv("AZURE_FOUNDRY_API_KEY")

    with pytest.raises(RuntimeError, match="AZURE_FOUNDRY_API_KEY"):
        _load_config()


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
