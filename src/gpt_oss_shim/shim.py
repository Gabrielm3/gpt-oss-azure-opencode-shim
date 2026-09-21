"""GPT-OSS on Azure + OpenCode compatibility shim.

A local HTTP shim between an OpenAI-compatible client (e.g. OpenCode) and an
Azure AI Foundry deployment of GPT-OSS. See ``docs/PROBLEM.md`` for the
recorded Azure behavior behind each fix.

1. Forced ``tool_choice``. Azure answers a forced function with HTTP 200 and
   ``choices: []`` (or an in-band error event when streaming), and answers
   ``"required"`` with HTTP 400 ``UnsupportedToolUse``. The shim rewrites any
   value other than ``"auto"``/``"none"`` to ``"auto"``.

2. Credentials. The client sends a placeholder key; the shim sends the real
   key as ``api-key`` and ``Authorization: Bearer``.

3. Headers. The shim sets its own ``Content-Type`` and drops the client's, so
   Azure never receives ``application/json,application/json`` (HTTP 400).

4. Local-only access. Requests from browsers (``Origin``, ``Sec-Fetch-Site``)
   or with a non-local ``Host`` get HTTP 403, because every forwarded request
   carries the real API key.

Environment variables
---------------------
UPSTREAM_URL
    Base URL of the Azure OpenAI-compatible endpoint, e.g.
    ``https://<resource>.services.ai.azure.com/openai``. Required.
AZURE_FOUNDRY_API_KEY
    API key for the Azure AI Foundry resource. Required.
SHIM_HOST
    Bind address (default: ``127.0.0.1``).
SHIM_PORT
    Bind port (default: ``9526``).
SHIM_LOG_LEVEL
    Uvicorn log level (default: ``info``).
SHIM_ALLOWED_HOSTS
    Comma-separated hostnames accepted in the ``Host`` header, in addition
    to ``localhost``, ``127.0.0.1`` and ``::1`` (default: empty).
SHIM_CONNECT_TIMEOUT
    Seconds to open a connection to the upstream (default: ``10``).
SHIM_READ_TIMEOUT
    Maximum seconds between bytes received from the upstream (default: ``600``).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

__version__ = "0.1.0"

logger = logging.getLogger("gpt_oss_shim")

# Hostnames a local client uses to reach the shim.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Upstream timeouts in seconds. Non-streamed completions send nothing until
# generation ends, so the default read timeout leaves room for long outputs.
_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_READ_TIMEOUT = 600.0
_WRITE_TIMEOUT = 30.0
_POOL_TIMEOUT = 10.0

# Hop-by-hop headers (RFC 9110 section 7.6.1) apply to one connection only.
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "transfer-encoding",
        "keep-alive",
        "proxy-authorization",
        "proxy-authenticate",
        "te",
        "trailer",
        "upgrade",
    }
)

# Headers we never forward upstream; either handled by us or unsafe.
_STRIPPED_REQUEST_HEADERS = _HOP_BY_HOP_HEADERS | {
    "host",
    "content-length",
    "content-type",
    "authorization",
    "accept-encoding",
    "api-key",
}

# Headers we never copy back to the client. httpx decodes the body, so the
# upstream length and encoding no longer describe what the client receives.
_STRIPPED_RESPONSE_HEADERS = _HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}

# tool_choice values that Azure accepts without issue.
_SAFE_TOOL_CHOICES = frozenset({"auto", "none"})


def _load_config() -> dict[str, Any]:
    """Read configuration from environment variables.

    Raises
    ------
    RuntimeError
        If a required variable (``UPSTREAM_URL`` or
        ``AZURE_FOUNDRY_API_KEY``) is missing, or a numeric variable is invalid.
    """
    upstream = os.environ.get("UPSTREAM_URL", "").rstrip("/")
    api_key = os.environ.get("AZURE_FOUNDRY_API_KEY", "").strip()

    if not upstream:
        raise RuntimeError("UPSTREAM_URL environment variable is required")
    if not api_key:
        raise RuntimeError("AZURE_FOUNDRY_API_KEY environment variable is required")

    extra_hosts = _parse_host_list(os.environ.get("SHIM_ALLOWED_HOSTS", ""))

    return {
        "upstream": upstream,
        "api_key": api_key,
        "host": os.environ.get("SHIM_HOST", "127.0.0.1"),
        "port": int(_env_number("SHIM_PORT", 9526)),
        "log_level": os.environ.get("SHIM_LOG_LEVEL", "info"),
        "allowed_hosts": _LOOPBACK_HOSTS | extra_hosts,
        "connect_timeout": _env_number("SHIM_CONNECT_TIMEOUT", _DEFAULT_CONNECT_TIMEOUT),
        "read_timeout": _env_number("SHIM_READ_TIMEOUT", _DEFAULT_READ_TIMEOUT),
    }


def _env_number(name: str, default: float) -> float:
    """Read a positive number from the environment, or fail with a clear error."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero, got {raw!r}")
    return value


def build_timeout(config: Mapping[str, Any]) -> httpx.Timeout:
    """Build upstream timeouts.

    ``read`` bounds the gap between received bytes, not the whole response,
    so a long streamed completion is fine while a stalled upstream is not.
    """
    return httpx.Timeout(
        connect=config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT),
        read=config.get("read_timeout", _DEFAULT_READ_TIMEOUT),
        write=_WRITE_TIMEOUT,
        pool=_POOL_TIMEOUT,
    )


def _parse_host_list(raw: str) -> frozenset[str]:
    """Parse a comma-separated hostname list into lowercase names."""
    return frozenset(name.strip().lower() for name in raw.split(",") if name.strip())


def is_loopback_bind(host: str) -> bool:
    """Return ``True`` if binding to ``host`` keeps the shim off the network."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _hostname(host_header: str) -> str:
    """Return the hostname of a ``Host`` header, without port or IPv6 brackets."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.split(":", 1)[0]


def rejection_reason(headers: Mapping[str, str], allowed_hosts: frozenset[str]) -> str | None:
    """Return why a request must be refused, or ``None`` if it may pass.

    The shim adds a real API key to every upstream call, so it only serves
    local, non-browser clients:

    - Browsers send ``Origin`` on cross-origin POSTs, including CORS "simple"
      requests that skip the preflight.
    - Browsers send ``Sec-Fetch-Site`` on every request; only ``none`` (the
      user typed the URL) is accepted.
    - A ``Host`` outside ``allowed_hosts`` means DNS rebinding or a request
      that was not addressed to this machine.
    """
    if "origin" in headers:
        return "requests with an Origin header are not accepted"
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site != "none":
        return f"browser requests with Sec-Fetch-Site={fetch_site!r} are not accepted"
    if _hostname(headers.get("host", "")) not in allowed_hosts:
        return "Host header is not an allowed local hostname"
    return None


class LocalOnlyMiddleware:
    """Reject browser-originated and non-local requests before any routing."""

    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            reason = rejection_reason(Headers(scope=scope), self.allowed_hosts)
            if reason is not None:
                logger.warning("rejected %s %s: %s", scope["method"], scope["path"], reason)
                response = JSONResponse(
                    status_code=403,
                    content={"error": {"message": reason, "type": "forbidden"}},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_upstream_headers(config: dict[str, Any]) -> dict[str, str]:
    """Build the headers injected on every upstream request."""
    return {
        "api-key": config["api_key"],
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
    }


def sanitize_chat_body(raw: bytes) -> tuple[bytes, list[str]]:
    """Rewrite a Chat Completions request body to work around Azure quirks.

    Returns
    -------
    tuple[bytes, list[str]]
        The sanitized body and a list of human-readable notes describing
        what was changed (empty list if nothing changed).
    """
    notes: list[str] = []
    if not raw:
        return raw, notes

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw, notes

    if not isinstance(payload, dict):
        return raw, notes

    choice = payload.get("tool_choice")
    if choice is not None:
        # ``choice`` may be a string ("auto"/"none"/"required") or a dict
        # ({"type": "function", ...}). Only strings can be safely compared
        # against the safe set; dicts always need rewriting.
        is_safe = isinstance(choice, str) and choice in _SAFE_TOOL_CHOICES
        if not is_safe:
            payload["tool_choice"] = "auto"
            notes.append(f"tool_choice={choice!r} -> 'auto'")

    return json.dumps(payload).encode("utf-8"), notes


def create_app(
    config: dict[str, Any] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Application factory.

    Parameters
    ----------
    config
        Optional pre-loaded configuration. If ``None``, the config is
        loaded from environment variables.
    transport
        Optional transport for the upstream client, e.g. ``httpx.MockTransport``
        in tests. Defaults to real network I/O.
    """
    if config is None:
        config = _load_config()

    # One client for the app's lifetime: connections to Azure are pooled and
    # reused instead of paying a TCP + TLS handshake on every request.
    client = httpx.AsyncClient(transport=transport, timeout=build_timeout(config))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(
        title="gpt-oss-azure-opencode-shim",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.http_client = client
    app.add_middleware(
        LocalOnlyMiddleware,
        allowed_hosts=config.get("allowed_hosts", _LOOPBACK_HOSTS),
    )

    upstream = config["upstream"]
    auth_headers = build_upstream_headers(config)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Lightweight liveness probe."""
        return {"status": "ok", "version": __version__}

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "OPTIONS", "HEAD"],
    )
    async def forward(path: str, request: Request) -> Response:
        """Forward any request to the upstream, applying compat fixes."""
        body = await request.body()

        if request.method == "POST" and path.endswith("chat/completions"):
            body, notes = sanitize_chat_body(body)
            for note in notes:
                logger.info("sanitized request: %s", note)

        forward_headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _STRIPPED_REQUEST_HEADERS
        }
        forward_headers.update(auth_headers)

        url = f"{upstream}/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        try:
            upstream_request = client.build_request(
                request.method,
                url,
                content=body,
                headers=forward_headers,
            )
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            return upstream_error_response(exc)

        headers = forwardable_response_headers(upstream_response.headers)

        if "text/event-stream" in upstream_response.headers.get("content-type", ""):
            return StreamingResponse(
                relay_sse(upstream_response),
                status_code=upstream_response.status_code,
                headers=headers,
            )

        try:
            content = await upstream_response.aread()
        except httpx.HTTPError as exc:
            return upstream_error_response(exc)
        finally:
            await upstream_response.aclose()

        return Response(
            content=content,
            status_code=upstream_response.status_code,
            headers=headers,
            media_type="application/json",  # used only if upstream sent no content-type
        )

    return app


def _upstream_error_payload(exc: httpx.HTTPError) -> tuple[int, dict[str, Any]]:
    """Map an httpx failure to an HTTP status and an OpenAI-style error body."""
    if isinstance(exc, httpx.TimeoutException):
        status, kind = 504, "upstream_timeout"
    else:
        status, kind = 502, "upstream_error"
    message = f"{kind.replace('_', ' ')}: {type(exc).__name__}"
    return status, {"error": {"message": message, "type": kind}}


def upstream_error_response(exc: httpx.HTTPError) -> JSONResponse:
    """Log an upstream failure and turn it into a 502 or 504 response."""
    logger.error("upstream request failed: %s: %s", type(exc).__name__, exc)
    status, payload = _upstream_error_payload(exc)
    return JSONResponse(status_code=status, content=payload)


def forwardable_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Upstream response headers that still hold after the shim relays the body.

    Keeps ``retry-after``, ``x-ratelimit-*`` and request IDs, which clients
    need for backoff and for correlating failures with Azure support.
    """
    return {k: v for k, v in headers.items() if k.lower() not in _STRIPPED_RESPONSE_HEADERS}


async def relay_sse(response: httpx.Response) -> AsyncIterator[bytes]:
    """Relay an upstream SSE body, ending with an error event if it breaks.

    The status line and headers are already sent when a stream breaks, so the
    failure can only be reported in-band. OpenAI-compatible clients (the
    OpenAI SDK, the AI SDK used by OpenCode) surface a ``data: {"error": ...}``
    event as an error instead of treating the truncated stream as complete.
    """
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    except httpx.HTTPError as exc:
        logger.error("upstream stream interrupted: %s: %s", type(exc).__name__, exc)
        _, payload = _upstream_error_payload(exc)
        # The leading blank line ends any event that was cut off mid-way.
        yield b"\n\ndata: " + json.dumps(payload).encode() + b"\n\n"
    finally:
        await response.aclose()


def main() -> int:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = _load_config()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    if not is_loopback_bind(config["host"]):
        logger.warning(
            "SHIM_HOST=%s is reachable from the network. The shim has no inbound "
            "authentication and adds the Azure API key to every request it forwards.",
            config["host"],
        )

    import uvicorn

    uvicorn.run(
        create_app(config),
        host=config["host"],
        port=config["port"],
        log_level=config["log_level"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
