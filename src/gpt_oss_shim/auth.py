"""Credentials the shim adds to every upstream request.

Two modes:

- API key (``AZURE_FOUNDRY_API_KEY`` set): a static key, sent as ``api-key``
  and ``Authorization: Bearer``.
- Entra ID (no key): a bearer token from ``azure-identity``'s
  ``DefaultAzureCredential``, e.g. an ``az login`` session or a managed
  identity. Needs the ``entra`` extra. Tokens are cached and refreshed shortly
  before they expire, because getting one can spawn ``az`` and take a second.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Protocol

# Token audience for Azure AI Services / Azure OpenAI data-plane calls.
ENTRA_SCOPE = "https://cognitiveservices.azure.com/.default"

# Refresh this many seconds before the token expires, so a request never
# leaves with a token that expires in flight.
_REFRESH_MARGIN = 300.0


class UpstreamAuthError(Exception):
    """The shim could not get a credential for the upstream request."""


class UpstreamAuth(Protocol):
    """Returns the auth headers for one upstream request."""

    async def headers(self) -> dict[str, str]: ...


class TokenCredential(Protocol):
    """The part of ``azure.core.credentials.TokenCredential`` the shim uses."""

    def get_token(self, *scopes: str) -> Any: ...


class ApiKeyAuth:
    """A static Azure API key."""

    def __init__(self, api_key: str) -> None:
        self._headers = {"api-key": api_key, "Authorization": f"Bearer {api_key}"}

    async def headers(self) -> dict[str, str]:
        return dict(self._headers)


class EntraAuth:
    """Entra ID bearer tokens, cached until shortly before they expire."""

    def __init__(
        self,
        credential: TokenCredential,
        *,
        scope: str = ENTRA_SCOPE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credential = credential
        self._scope = scope
        self._clock = clock
        self._token: str | None = None
        self._expires_on = 0.0
        self._lock = asyncio.Lock()

    async def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._current_token()}"}

    async def _current_token(self) -> str:
        async with self._lock:
            if self._token is None or self._clock() >= self._expires_on - _REFRESH_MARGIN:
                try:
                    # The sync credential blocks (it may run `az`), so keep it
                    # off the event loop.
                    access = await asyncio.to_thread(self._credential.get_token, self._scope)
                except Exception as exc:
                    raise UpstreamAuthError(f"{type(exc).__name__}: {exc}") from exc
                self._token = access.token
                self._expires_on = float(access.expires_on)
            return self._token


def default_entra_credential() -> TokenCredential:
    """Build ``DefaultAzureCredential``, or fail with install instructions."""
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError(
            "AZURE_FOUNDRY_API_KEY is not set, so the shim uses Entra ID auth, which "
            "needs the entra extra: pip install 'gpt-oss-azure-opencode-shim[entra]'"
        ) from exc
    credential: TokenCredential = DefaultAzureCredential()
    return credential
