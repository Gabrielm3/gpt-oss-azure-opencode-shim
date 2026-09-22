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

5. Tool-call polyfill. After the rewrite, the model sometimes misses the tool
   call: it writes the answer as JSON text, or leaks the call's arguments into
   its reasoning. For forced requests the shim buffers the answer and, if the
   JSON matches exactly one candidate tool schema, returns it as that tool
   call (see ``polyfill.py``).

6. Outcomes. Every forwarded request gets an ``x-shim-outcome`` header and a
   count in ``/metrics`` (Prometheus); a ``choices: []`` answer becomes 502.

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
    Log level for the shim and uvicorn (default: ``info``). HTTP client
    request lines, which contain the upstream URL, appear only at ``debug``.
SHIM_ALLOWED_HOSTS
    Comma-separated hostnames accepted in the ``Host`` header, in addition
    to ``localhost``, ``127.0.0.1`` and ``::1`` (default: empty).
SHIM_CONNECT_TIMEOUT
    Seconds to open a connection to the upstream (default: ``10``).
SHIM_READ_TIMEOUT
    Maximum seconds between bytes received from the upstream (default: ``600``).
SHIM_TOOL_POLYFILL
    ``on`` (default): repair answers to forced requests into the tool call.
    ``observe``: leave answers unchanged and only count what a repair would do
    (``shim_polyfill_observed_total``). ``off``: rewrite ``tool_choice`` only.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from .metrics import Metrics
from .polyfill import (
    ForcedToolChoice,
    Outcome,
    PolyfillMode,
    forced_tool_choice,
    repair_completion,
    repair_stream,
)
from .traces import DEFAULT_TRACE_OUTCOMES, TraceWriter

__version__ = "0.2.1"

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

# Response header that tells the client what the shim did with its request.
OUTCOME_HEADER = "x-shim-outcome"


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
        "tool_polyfill": _env_polyfill_mode("SHIM_TOOL_POLYFILL"),
        "trace_dir": _env_path("SHIM_TRACE_DIR"),
        "trace_outcomes": _env_outcomes("SHIM_TRACE_OUTCOMES", DEFAULT_TRACE_OUTCOMES),
    }


def _env_path(name: str) -> Path | None:
    """Read an optional directory path from the environment."""
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def _env_outcomes(name: str, default: frozenset[Outcome]) -> frozenset[Outcome]:
    """Read a comma-separated list of outcome names, or fail with a clear error."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    known = {outcome.value: outcome for outcome in Outcome}
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    unknown = [part for part in names if part not in known]
    if unknown:
        raise RuntimeError(f"{name} has unknown outcomes {unknown}; use {sorted(known)}")
    return frozenset(known[part] for part in names)


_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _env_polyfill_mode(name: str) -> PolyfillMode:
    """Read ``on``, ``observe`` or ``off`` from the environment (default ``on``)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return PolyfillMode.ON
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        return PolyfillMode.ON
    if word in _FALSE_WORDS:
        return PolyfillMode.OFF
    if word == PolyfillMode.OBSERVE.value:
        return PolyfillMode.OBSERVE
    raise RuntimeError(f"{name} must be on, observe or off, got {raw!r}")


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
    metrics = Metrics()
    app.state.http_client = client
    app.state.metrics = metrics
    app.add_middleware(
        LocalOnlyMiddleware,
        allowed_hosts=config.get("allowed_hosts", _LOOPBACK_HOSTS),
    )

    upstream = config["upstream"]
    auth_headers = build_upstream_headers(config)
    mode = PolyfillMode(config.get("tool_polyfill", PolyfillMode.ON))
    trace_dir = config.get("trace_dir")
    traces = (
        TraceWriter(Path(trace_dir), config.get("trace_outcomes", DEFAULT_TRACE_OUTCOMES))
        if trace_dir
        else None
    )

    def trace(outcome: Outcome, raw: bytes, request: dict[str, Any], *, is_sse: bool) -> None:
        if traces is not None:
            traces.write(outcome=outcome, mode=mode, stream=is_sse, request=request, response=raw)

    def finish(
        response: Response, outcome: Outcome, forced_seconds: float | None = None
    ) -> Response:
        response.headers[OUTCOME_HEADER] = outcome.value
        metrics.record(outcome, forced_seconds=forced_seconds)
        return response

    def observe(
        content: bytes, forced: ForcedToolChoice, request: dict[str, Any], *, is_sse: bool
    ) -> None:
        """Count what a repair would do, without changing the answer."""
        repair = repair_stream if is_sse else repair_completion
        _, outcome = repair(content, forced)
        metrics.record_observed(outcome)
        trace(outcome, content, request, is_sse=is_sse)
        logger.info("observed forced tool_choice outcome=%s (answer unchanged)", outcome.value)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Lightweight liveness probe."""
        return {"status": "ok", "version": __version__}

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        """Prometheus metrics for this shim instance."""
        return Response(content=metrics.render(), media_type=metrics.content_type)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "OPTIONS", "HEAD"],
    )
    async def forward(path: str, request: Request) -> Response:
        """Forward any request to the upstream, applying compat fixes."""
        started = time.perf_counter()
        body = await request.body()
        is_chat = request.method == "POST" and path.endswith("chat/completions")
        forced: ForcedToolChoice | None = None
        payload: dict[str, Any] = {}
        default_outcome = Outcome.PASSTHROUGH

        if is_chat:
            payload = _json_object(body) or {}
            if mode is not PolyfillMode.OFF:
                forced = forced_tool_choice(payload)
            body, notes = sanitize_chat_body(body)
            for note in notes:
                logger.info("sanitized request: %s", note)
            if notes:
                default_outcome = Outcome.REWRITTEN

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
            return finish(upstream_error_response(exc), Outcome.UPSTREAM_ERROR)

        headers = forwardable_response_headers(upstream_response.headers)
        is_sse = "text/event-stream" in upstream_response.headers.get("content-type", "")
        ok = upstream_response.status_code == 200
        observing = forced is not None and ok and mode is PolyfillMode.OBSERVE

        if is_sse and (forced is None or observing):
            on_complete = None
            if observing:
                observed_forced, observed_payload = forced, payload

                def on_complete(body: bytes) -> None:
                    observe(body, observed_forced, observed_payload, is_sse=True)

            response = StreamingResponse(
                relay_sse(upstream_response, on_complete=on_complete),
                status_code=upstream_response.status_code,
                headers=headers,
            )
            return finish(response, default_outcome)

        try:
            content = await upstream_response.aread()
        except httpx.HTTPError as exc:
            return finish(upstream_error_response(exc), Outcome.UPSTREAM_ERROR)
        finally:
            await upstream_response.aclose()

        if observing:
            observe(content, forced, payload, is_sse=False)
        elif forced is not None and ok:
            # The whole answer is needed before it can be checked against the
            # tool schemas, so forced requests are buffered, streamed or not.
            repair = repair_stream if is_sse else repair_completion
            raw = content
            content, outcome = repair(raw, forced)
            trace(outcome, raw, payload, is_sse=is_sse)
            elapsed = time.perf_counter() - started
            logger.info("forced tool_choice outcome=%s seconds=%.2f", outcome.value, elapsed)
            if outcome is Outcome.EMPTY_CHOICES and not is_sse:
                return finish(empty_choices_response(headers), outcome, elapsed)
            response = Response(content=content, status_code=200, headers=headers)
            return finish(response, outcome, elapsed)

        if is_chat and ok and not is_sse and _is_empty_completion(content):
            logger.error("upstream returned HTTP 200 with no choices")
            return finish(empty_choices_response(headers), Outcome.EMPTY_CHOICES)

        response = Response(
            content=content,
            status_code=upstream_response.status_code,
            headers=headers,
            media_type="application/json",  # used only if upstream sent no content-type
        )
        return finish(response, default_outcome)

    return app


def _json_object(body: bytes) -> dict[str, Any] | None:
    """Parse a request body as a JSON object, or return ``None``."""
    try:
        payload = json.loads(body) if body else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_empty_completion(content: bytes) -> bool:
    """Return ``True`` for a Chat Completions body with ``choices: []``."""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("choices") == []


def empty_choices_response(headers: Mapping[str, str]) -> JSONResponse:
    """Turn a silent ``choices: []`` answer into an explicit 502 error.

    Upstream headers (request IDs, rate limits) are kept so the failure can
    still be correlated with the provider.
    """
    kept = {k: v for k, v in headers.items() if k.lower() != "content-type"}
    return JSONResponse(
        status_code=502,
        headers=kept,
        content={
            "error": {
                "message": "upstream returned HTTP 200 with no choices",
                "type": Outcome.EMPTY_CHOICES.value,
            }
        },
    )


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


async def relay_sse(
    response: httpx.Response,
    *,
    on_complete: Callable[[bytes], None] | None = None,
) -> AsyncIterator[bytes]:
    """Relay an upstream SSE body, ending with an error event if it breaks.

    The status line and headers are already sent when a stream breaks, so the
    failure can only be reported in-band. OpenAI-compatible clients (the
    OpenAI SDK, the AI SDK used by OpenCode) surface a ``data: {"error": ...}``
    event as an error instead of treating the truncated stream as complete.

    ``on_complete`` receives a copy of the whole body once the stream ends
    normally. It is not called for a broken stream.
    """
    copy: list[bytes] = []
    try:
        async for chunk in response.aiter_bytes():
            if on_complete is not None:
                copy.append(chunk)
            yield chunk
    except httpx.HTTPError as exc:
        logger.error("upstream stream interrupted: %s: %s", type(exc).__name__, exc)
        _, payload = _upstream_error_payload(exc)
        # The leading blank line ends any event that was cut off mid-way.
        yield b"\n\ndata: " + json.dumps(payload).encode() + b"\n\n"
    else:
        if on_complete is not None:
            try:
                on_complete(b"".join(copy))
            except Exception:
                # Measuring must never break the answer the client already has.
                logger.exception("failed to evaluate a streamed answer in observe mode")
    finally:
        await response.aclose()


_LOG_LEVELS = {"critical", "error", "warning", "info", "debug", "trace"}


def configure_logging(level: str) -> None:
    """Apply ``SHIM_LOG_LEVEL`` to the shim logger and quiet the HTTP client.

    httpx logs every request line at INFO, and that line holds the upstream
    URL with the Azure resource name. It stays at WARNING unless debugging, so
    logs pasted into an issue do not reveal the resource.
    """
    name = level.strip().lower()
    if name not in _LOG_LEVELS:
        raise RuntimeError(f"SHIM_LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}, got {level!r}")
    numeric = logging.DEBUG if name == "trace" else getattr(logging, name.upper())
    logger.setLevel(numeric)
    client_level = logging.DEBUG if numeric <= logging.DEBUG else logging.WARNING
    for client_logger in ("httpx", "httpcore"):
        logging.getLogger(client_logger).setLevel(client_level)


def main() -> int:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = _load_config()
        configure_logging(config["log_level"])
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
