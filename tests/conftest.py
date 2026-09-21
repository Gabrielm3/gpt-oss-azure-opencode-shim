"""Shared fixtures: an app wired to ``httpx.MockTransport`` and a client for it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gpt_oss_shim.shim import create_app

Handler = Callable[[httpx.Request], httpx.Response]

FAKE_CONFIG: dict[str, Any] = {
    "upstream": "https://upstream.test/openai",
    "api_key": "test-key-123",
    "host": "127.0.0.1",
    "port": 9526,
    "log_level": "info",
}

# The shim only accepts loopback Host headers, so tests talk to it as a local client would.
SHIM_BASE_URL = "http://127.0.0.1:9526"


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


@pytest.fixture
def make_app() -> Callable[..., FastAPI]:
    """Build an app whose upstream calls go to ``handler``."""

    def _make(handler: Handler = ok_handler, **overrides: Any) -> FastAPI:
        config = {**FAKE_CONFIG, **overrides, "transport": httpx.MockTransport(handler)}
        return create_app(config)

    return _make


@pytest.fixture
def shim_client(
    make_app: Callable[..., FastAPI],
) -> Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]:
    """Open an HTTP client against an app whose upstream is ``handler``."""

    @asynccontextmanager
    async def _client(
        handler: Handler = ok_handler, **overrides: Any
    ) -> AsyncIterator[httpx.AsyncClient]:
        app = make_app(handler, **overrides)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=SHIM_BASE_URL) as client:
            yield client

    return _client
