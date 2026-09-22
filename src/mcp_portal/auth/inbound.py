"""RFC 9068 access-token validation and RFC 9728/8414 discovery for
`transport: http` (§8). The `mcp` SDK supplies the resource-server routing,
Origin/Host defense, and 401/`WWW-Authenticate` shape (see this plan's
Architecture section); this module supplies the one IdP-specific piece the
SDK deliberately leaves to the operator: verifying a bearer token against a
JWKS and turning it into an `AccessToken`.
"""

import asyncio
import time
from typing import Any

import httpx

_JWKS_REFRESH_INTERVAL_S = 60.0
_DISCOVERY_SUFFIXES = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
)


class InboundAuthError(Exception):
    """Raised when a bearer token cannot be validated at all (startup-time
    discovery failures) — as opposed to an individual token simply being
    invalid, which `JwtTokenVerifier.verify_token` reports by returning
    `None` so the SDK's bearer middleware can answer with a 401."""


async def discover_jwks_uri(client: httpx.AsyncClient, issuer: str) -> str:
    """Discover `jwks_uri` from `issuer`'s AS metadata, RFC 8414 first, then
    OpenID Connect discovery — both are in the wild for the same issuer."""
    base = issuer.rstrip("/")
    for suffix in _DISCOVERY_SUFFIXES:
        try:
            response = await client.get(base + suffix)
        except httpx.RequestError:
            continue
        if response.status_code != 200:
            continue
        jwks_uri = response.json().get("jwks_uri")
        if isinstance(jwks_uri, str):
            return jwks_uri
    raise InboundAuthError(f"could not discover jwks_uri for issuer {issuer!r}")


class JwksCache:
    """Caches JWKS keys by `kid`. An unknown `kid` triggers a refresh, rate
    limited to once per 60s so a forged `kid` cannot drive unbounded fetches
    (§8)."""

    def __init__(self, client: httpx.AsyncClient, jwks_uri: str) -> None:
        self._client = client
        self._jwks_uri = jwks_uri
        self._keys: dict[str, dict[str, Any]] = {}
        self._last_refresh: float = 0.0
        self._lock = asyncio.Lock()

    async def key_for(self, kid: str) -> dict[str, Any] | None:
        key = self._keys.get(kid)
        if key is not None:
            return key
        await self._maybe_refresh()
        return self._keys.get(kid)

    async def _maybe_refresh(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now - self._last_refresh < _JWKS_REFRESH_INTERVAL_S:
                return
            response = await self._client.get(self._jwks_uri)
            response.raise_for_status()
            for jwk in response.json().get("keys", []):
                kid = jwk.get("kid")
                if isinstance(kid, str):
                    self._keys[kid] = jwk
            self._last_refresh = now
