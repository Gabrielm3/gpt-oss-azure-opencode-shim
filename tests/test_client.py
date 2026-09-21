"""Tests for the shared upstream HTTP client."""

from __future__ import annotations

import httpx

from tests.conftest import SHIM_BASE_URL


async def test_one_client_is_reused_across_requests(make_app) -> None:
    app = make_app()
    client_before = app.state.http_client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=SHIM_BASE_URL) as client:
        await client.get("/v1/models")
        await client.get("/v1/models")

    assert app.state.http_client is client_before
    assert not client_before.is_closed


async def test_client_is_closed_on_shutdown(make_app) -> None:
    app = make_app()

    async with app.router.lifespan_context(app):
        assert not app.state.http_client.is_closed

    assert app.state.http_client.is_closed


def test_client_uses_configured_timeouts(make_app) -> None:
    app = make_app(connect_timeout=3.0, read_timeout=45.0)

    timeout = app.state.http_client.timeout

    assert timeout.connect == 3.0
    assert timeout.read == 45.0
    assert timeout.write is not None
    assert timeout.pool is not None
