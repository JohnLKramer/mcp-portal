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
import jwt as pyjwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

_JWKS_REFRESH_INTERVAL_S = 60.0
_ACCEPTED_TYP = frozenset({"at+jwt", "jwt"})
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


def _scopes_from_claims(claims: dict[str, Any]) -> list[str]:
    """Scopes come from a space-delimited `scope` string or an `scp` array —
    both are in the wild (§8)."""
    if isinstance(claims.get("scp"), list):
        return [str(s) for s in claims["scp"]]
    scope = claims.get("scope")
    if isinstance(scope, str) and scope:
        return scope.split()
    return []


class JwtTokenVerifier(TokenVerifier):
    """Validates an RFC 9068 access token against a JWKS.

    A malformed `authorization_details` claim, an unrecognized `kid`, a
    disallowed `alg` (including `none`, unconditionally), a wrong `typ`, or
    a failed `iss`/`aud`/`exp`/`nbf`/scope check all deny the request by
    returning `None` — the SDK's `BearerAuthBackend` turns that into a 401.
    None of these distinctions are surfaced to the caller (§10: never leak
    token contents), only the fact of denial.
    """

    def __init__(
        self,
        issuer: str,
        audience: str,
        algorithms: list[str],
        required_scopes: list[str],
        leeway_s: float,
        jwks: JwksCache,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._algorithms = frozenset(algorithms) - {"none"}
        self._required_scopes = required_scopes
        self._leeway_s = leeway_s
        self._jwks = jwks

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = pyjwt.get_unverified_header(token)
        except pyjwt.InvalidTokenError:
            return None

        alg = header.get("alg")
        if alg not in self._algorithms:
            return None
        if header.get("typ", "").lower() not in _ACCEPTED_TYP:
            return None

        kid = header.get("kid")
        if not isinstance(kid, str):
            return None
        jwk = await self._jwks.key_for(kid)
        if jwk is None:
            return None

        try:
            key = pyjwt.PyJWK.from_dict(jwk, algorithm=alg).key
            claims = pyjwt.decode(
                token,
                key=key,
                algorithms=[alg],
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway_s,
                options={"require": ["exp", "iss", "aud"]},
            )
        except pyjwt.PyJWTError:
            # Broader than InvalidTokenError: PyJWK.from_dict can raise
            # InvalidKeyError (e.g. a JWK whose `kty` doesn't match `alg`,
            # such as a misconfigured HS* allowlist paired with an RSA JWK),
            # which is not an InvalidTokenError subclass. Any JWT-related
            # failure here must deny the request, not crash the request.
            return None

        raw_details = claims.get("authorization_details")
        if raw_details is not None and not isinstance(raw_details, list):
            return None  # a malformed claim denies the request rather than being ignored (§8)

        scopes = _scopes_from_claims(claims)
        if any(scope not in scopes for scope in self._required_scopes):
            return None

        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("sub") or "unknown"),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self._audience,
            subject=claims.get("sub"),
            claims=claims,
        )
