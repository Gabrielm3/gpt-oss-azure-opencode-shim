"""Tests for the FastAPI application factory and forwarding behavior.

Uses ``httpx.MockTransport`` to intercept the shim's outbound HTTP calls
without monkeypatching internals. This exercises the real app code path.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from httpx import ASGITransport, AsyncClient

from gpt_oss_shim.shim import create_app

FAKE_CONFIG_BASE = {
    "upstream": "https://upstream.test/openai",
    "api_key": "test-key-123",
    "host": "127.0.0.1",
    "port": 9526,
    "log_level": "info",
}


def _build_app(handler) -> Any:  # noqa: ANN001
    """Return an app wired to a MockTransport with the given handler."""
    transport = httpx.MockTransport(handler)
    config = {**FAKE_CONFIG_BASE, "transport": transport}
    return create_app(config)


async def test_healthz_responds() -> None:
    app = _build_app(lambda req: httpx.Response(200, json={"unused": True}))
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


async def test_forward_injects_auth_and_strips_content_type() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200, json={"forwarded": True})

    app = _build_app(handler)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [], "tool_choice": "required"},
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json() == {"forwarded": True}

    headers = captured["headers"]
    assert headers["api-key"] == "test-key-123"
    assert headers["authorization"] == "Bearer test-key-123"
    assert headers["content-type"] == "application/json"
    assert headers["accept-encoding"] == "identity"

    assert captured["method"] == "POST"
    assert str(captured["url"]).startswith("https://upstream.test/openai/")


async def test_forward_rewrites_forced_tool_choice() -> None:
    captured_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    app = _build_app(handler)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [],
                "tool_choice": {"type": "function", "function": {"name": "x"}},
            },
        )

    assert captured_body["tool_choice"] == "auto"


async def test_forward_preserves_safe_tool_choice() -> None:
    captured_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    app = _build_app(handler)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [], "tool_choice": "auto"},
        )

    assert captured_body["tool_choice"] == "auto"


async def test_forward_returns_502_on_upstream_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream unreachable", request=request)

    app = _build_app(handler)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": []},
        )

    assert response.status_code == 502
    body = response.json()
    assert "error" in body
