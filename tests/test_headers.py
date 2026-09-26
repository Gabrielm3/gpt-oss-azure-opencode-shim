"""Tests for upstream headers and credentials."""

from __future__ import annotations

import builtins
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gpt_oss_shim.auth import (
    ENTRA_SCOPE,
    ApiKeyAuth,
    EntraAuth,
    UpstreamAuthError,
    default_entra_credential,
)
from gpt_oss_shim.shim import UPSTREAM_HEADERS, build_upstream_auth, create_app
from tests.conftest import FAKE_CONFIG, SHIM_BASE_URL


class FakeCredential:
    """Hands out numbered tokens that expire ``lifetime`` seconds from ``now``."""

    def __init__(self, clock: Callable[[], float], lifetime: float = 3600) -> None:
        self.clock = clock
        self.lifetime = lifetime
        self.calls: list[tuple[str, ...]] = []
        self.threads: list[str] = []

    def get_token(self, *scopes: str) -> Any:
        self.calls.append(scopes)
        self.threads.append(threading.current_thread().name)
        return SimpleNamespace(
            token=f"token-{len(self.calls)}", expires_on=int(self.clock() + self.lifetime)
        )


class BrokenCredential:
    def get_token(self, *scopes: str) -> Any:
        raise RuntimeError("az login expired")


def test_headers_force_json_content_type() -> None:
    assert UPSTREAM_HEADERS["Content-Type"] == "application/json"


def test_headers_disable_compression_for_sse() -> None:
    assert UPSTREAM_HEADERS["Accept-Encoding"] == "identity"


async def test_api_key_is_sent_as_api_key_and_bearer() -> None:
    headers = await ApiKeyAuth("test-key").headers()

    assert headers == {"api-key": "test-key", "Authorization": "Bearer test-key"}


async def test_entra_sends_only_a_bearer_token() -> None:
    now = 1000.0
    credential = FakeCredential(lambda: now)

    headers = await EntraAuth(credential, clock=lambda: now).headers()

    assert headers == {"Authorization": "Bearer token-1"}
    assert credential.calls == [(ENTRA_SCOPE,)]
    assert credential.threads[0] != threading.main_thread().name


async def test_entra_reuses_the_token_until_close_to_expiry() -> None:
    now = [1000.0]
    credential = FakeCredential(lambda: now[0])
    auth = EntraAuth(credential, clock=lambda: now[0])

    await auth.headers()
    now[0] += 3600 - 301  # still more than the 300 s margin left
    assert (await auth.headers())["Authorization"] == "Bearer token-1"

    now[0] += 2  # inside the margin
    assert (await auth.headers())["Authorization"] == "Bearer token-2"
    assert len(credential.calls) == 2


async def test_entra_failure_is_an_upstream_auth_error() -> None:
    with pytest.raises(UpstreamAuthError, match="az login expired"):
        await EntraAuth(BrokenCredential()).headers()


class FlakyCredential(FakeCredential):
    """Works once, then fails, like an ``az`` hiccup during refresh."""

    def get_token(self, *scopes: str) -> Any:
        if self.calls:
            self.calls.append(scopes)
            raise RuntimeError("az timed out")
        return super().get_token(*scopes)


async def test_failed_refresh_keeps_a_token_that_has_not_expired() -> None:
    now = [1000.0]
    credential = FlakyCredential(lambda: now[0])
    auth = EntraAuth(credential, clock=lambda: now[0])
    await auth.headers()

    now[0] += 3600 - 60  # inside the refresh margin, still valid
    assert (await auth.headers())["Authorization"] == "Bearer token-1"

    now[0] += 60  # expired: nothing left to fall back on
    with pytest.raises(UpstreamAuthError, match="az timed out"):
        await auth.headers()


def test_build_upstream_auth_prefers_the_api_key() -> None:
    assert isinstance(build_upstream_auth({"api_key": "k"}), ApiKeyAuth)


def test_build_upstream_auth_uses_entra_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gpt_oss_shim.shim.default_entra_credential", lambda: FakeCredential(lambda: 0.0)
    )

    assert isinstance(build_upstream_auth({"api_key": None}), EntraAuth)


def test_entra_without_the_extra_fails_with_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def no_azure(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("azure"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_azure)

    with pytest.raises(RuntimeError, match=r"\[entra\]"):
        default_entra_credential()


def test_default_entra_credential_is_default_azure_credential() -> None:
    from azure.identity import DefaultAzureCredential

    assert isinstance(default_entra_credential(), DefaultAzureCredential)


def _entra_app(handler: Callable[[httpx.Request], httpx.Response], credential: Any) -> FastAPI:
    return create_app(
        {**FAKE_CONFIG, "api_key": None},
        transport=httpx.MockTransport(handler),
        auth=EntraAuth(credential),
    )


async def test_entra_token_reaches_the_upstream_without_api_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    app = _entra_app(handler, FakeCredential(lambda: 1000.0))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=SHIM_BASE_URL
    ) as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer placeholder"})

    assert response.status_code == 200
    assert seen[0].headers["authorization"] == "Bearer token-1"
    assert "api-key" not in seen[0].headers


async def test_credential_failure_returns_502_and_counts_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may leave without credentials")

    app = _entra_app(handler, BrokenCredential())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=SHIM_BASE_URL
    ) as client:
        response = await client.post("/v1/chat/completions", json={"model": "m", "messages": []})
        metrics = (await client.get("/metrics")).text

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_auth_error"
    assert "az login expired" not in response.text
    assert response.headers["x-shim-outcome"] == "upstream_error"
    assert 'shim_requests_total{outcome="upstream_error"} 1.0' in metrics
