"""Tests for the local-only request guard.

The shim injects a real API key into every upstream request, so it must not
be usable from a web page (CSRF via CORS simple requests) or through DNS
rebinding (a foreign hostname resolving to 127.0.0.1).
"""

from __future__ import annotations

import httpx
import pytest

from gpt_oss_shim.shim import is_loopback_bind


class RecordingUpstream:
    """Upstream stand-in that counts how many requests reached it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json={"ok": True})


async def test_request_with_origin_header_is_rejected(shim_client) -> None:
    upstream = RecordingUpstream()

    async with shim_client(upstream) as client:
        response = await client.post(
            "/v1/chat/completions",
            content=b'{"model": "m", "messages": []}',
            headers={"Content-Type": "text/plain", "Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    assert "error" in response.json()
    assert upstream.calls == 0


async def test_cross_site_fetch_metadata_is_rejected(shim_client) -> None:
    upstream = RecordingUpstream()

    async with shim_client(upstream) as client:
        response = await client.get("/v1/models", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403
    assert upstream.calls == 0


async def test_user_initiated_navigation_is_allowed(shim_client) -> None:
    async with shim_client() as client:
        response = await client.get("/healthz", headers={"Sec-Fetch-Site": "none"})

    assert response.status_code == 200


async def test_foreign_host_header_is_rejected(shim_client) -> None:
    upstream = RecordingUpstream()

    async with shim_client(upstream) as client:
        response = await client.get("/v1/models", headers={"Host": "attacker.example:9526"})

    assert response.status_code == 403
    assert upstream.calls == 0


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:9526", "localhost:9526", "LOCALHOST", "[::1]:9526", "127.0.0.1"],
)
async def test_loopback_host_headers_are_allowed(shim_client, host: str) -> None:
    upstream = RecordingUpstream()

    async with shim_client(upstream) as client:
        response = await client.get("/v1/models", headers={"Host": host})

    assert response.status_code == 200
    assert upstream.calls == 1


@pytest.mark.parametrize(
    ("bind", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.10", False),
    ],
)
def test_is_loopback_bind(bind: str, expected: bool) -> None:
    assert is_loopback_bind(bind) is expected


async def test_configured_extra_host_is_allowed(shim_client) -> None:
    allowed = frozenset({"localhost", "127.0.0.1", "::1", "shim.internal"})

    async with shim_client(allowed_hosts=allowed) as client:
        response = await client.get("/v1/models", headers={"Host": "shim.internal:9526"})

    assert response.status_code == 200
