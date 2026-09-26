"""Tests for the FastAPI application factory and forwarding behavior.

Uses ``httpx.MockTransport`` (see ``conftest.py``) to intercept the shim's
outbound HTTP calls without monkeypatching internals. This exercises the
real app code path.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


async def test_healthz_responds(shim_client) -> None:
    async with shim_client() as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


async def test_forward_injects_auth_and_strips_content_type(shim_client) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200, json={"forwarded": True})

    async with shim_client(handler) as client:
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


async def test_forward_rewrites_forced_tool_choice(shim_client) -> None:
    captured_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    async with shim_client(handler) as client:
        await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-oss-120b",
                "messages": [],
                "tool_choice": {"type": "function", "function": {"name": "x"}},
            },
        )

    assert captured_body["tool_choice"] == "auto"


async def test_forward_preserves_safe_tool_choice(shim_client) -> None:
    captured_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    async with shim_client(handler) as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [], "tool_choice": "auto"},
        )

    assert captured_body["tool_choice"] == "auto"


async def test_forward_returns_502_on_upstream_failure(shim_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream unreachable", request=request)

    async with shim_client(handler) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": []},
        )

    assert response.status_code == 502
    body = response.json()
    assert "error" in body
