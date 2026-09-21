"""GPT-OSS on Azure + OpenCode compatibility shim.

A lightweight HTTP shim that sits between the OpenCode CLI and an
Azure AI Foundry deployment of an OpenAI-compatible model (e.g. GPT-OSS).
It fixes three silent incompatibilities that cause OpenCode to hang:

1. Missing ``api-key`` header. The OpenAI-compatible SDK used by OpenCode
   sends ``Authorization: Bearer``, but Azure AI Foundry expects ``api-key``.
   The shim injects both.

2. Duplicated ``Content-Type`` header. If the client also sets
   ``Content-Type``, the upstream receives ``application/json,application/json``
   and rejects the request. The shim strips client-provided content-type
   before forwarding.

3. Unsupported ``tool_choice`` values. Azure silently ignores forced
   ``tool_choice`` (object or ``"required"``) and returns an empty
   ``choices: []`` array, which causes OpenCode to hang. The shim rewrites
   any non-``auto``/``none`` value to ``"auto"`` before forwarding.

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
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

__version__ = "0.1.0"

logger = logging.getLogger("gpt_oss_shim")

# Headers we never forward upstream; either handled by us or unsafe.
_STRIPPED_REQUEST_HEADERS = frozenset({
    "host",
    "content-length",
    "content-type",
    "authorization",
    "accept-encoding",
    "api-key",
    "connection",
    "transfer-encoding",
    "keep-alive",
    "proxy-authorization",
    "proxy-authenticate",
    "te",
    "trailer",
    "upgrade",
})

# tool_choice values that Azure accepts without issue.
_SAFE_TOOL_CHOICES = frozenset({"auto", "none"})


def _load_config() -> dict[str, Any]:
    """Read configuration from environment variables.

    Raises
    ------
    RuntimeError
        If a required variable (``UPSTREAM_URL`` or
        ``AZURE_FOUNDRY_API_KEY``) is missing.
    """
    upstream = os.environ.get("UPSTREAM_URL", "").rstrip("/")
    api_key = os.environ.get("AZURE_FOUNDRY_API_KEY", "").strip()

    if not upstream:
        raise RuntimeError("UPSTREAM_URL environment variable is required")
    if not api_key:
        raise RuntimeError("AZURE_FOUNDRY_API_KEY environment variable is required")

    return {
        "upstream": upstream,
        "api_key": api_key,
        "host": os.environ.get("SHIM_HOST", "127.0.0.1"),
        "port": int(os.environ.get("SHIM_PORT", "9526")),
        "log_level": os.environ.get("SHIM_LOG_LEVEL", "info"),
    }


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


def create_app(config: dict[str, Any] | None = None) -> FastAPI:
    """Application factory.

    Parameters
    ----------
    config
        Optional pre-loaded configuration. If ``None``, the config is
        loaded from environment variables.
    """
    if config is None:
        config = _load_config()

    transport = config.get("transport")

    app = FastAPI(
        title="gpt-oss-azure-opencode-shim",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
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
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _STRIPPED_REQUEST_HEADERS
        }
        forward_headers.update(auth_headers)

        url = f"{upstream}/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        if transport is not None:
            client = httpx.AsyncClient(transport=transport, timeout=None)
        else:
            client = httpx.AsyncClient(timeout=None)
        try:
            upstream_request = client.build_request(
                request.method,
                url,
                content=body,
                headers=forward_headers,
            )
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            logger.error("upstream error: %s", exc)
            return JSONResponse(
                status_code=502,
                content={"error": {"message": f"upstream error: {exc}"}},
            )

        content_type = upstream_response.headers.get("content-type", "")

        if "text/event-stream" in content_type:
            async def event_stream():
                try:
                    async for chunk in upstream_response.aiter_raw():
                        yield chunk
                finally:
                    await upstream_response.aclose()
                    await client.aclose()

            return StreamingResponse(
                event_stream(),
                status_code=upstream_response.status_code,
                media_type="text/event-stream",
            )

        content = await upstream_response.aread()
        await upstream_response.aclose()
        await client.aclose()
        return Response(
            content=content,
            status_code=upstream_response.status_code,
            media_type=content_type or "application/json",
        )

    return app


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
