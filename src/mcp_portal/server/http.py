"""Serve the tool set over streamable HTTP (§8's "Streamable HTTP transport"
and "Inbound (HTTP transport)").

Routing, the Origin/Host defense, RFC 9728 discovery and the
401/`WWW-Authenticate` shape all come from the `mcp` SDK — `Server.streamable_http_app`
assembles exactly that stack from `TransportSecuritySettings`, `AuthSettings`
and a `TokenVerifier`, and reimplementing any of it here would only be a second,
worse copy. This module supplies the two pieces the SDK deliberately leaves to
the operator: `JwtTokenVerifier` (the IdP-specific token check) and the
per-request `Principal` a policy decision is evaluated against.
"""

import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from urllib.parse import urlparse

import httpx
from mcp import types
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import DEFAULT_MAX_SESSIONS, DEFAULT_SESSION_IDLE_TIMEOUT
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from starlette.applications import Starlette

from mcp_portal.app import App
from mcp_portal.auth.inbound import InboundAuthError, JwksCache, JwtTokenVerifier, discover_jwks_uri
from mcp_portal.auth.principal import Principal, local_principal, principal_from_access_token
from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import LOOPBACK_HOSTS, Config, InboundAuthConfig

log = logging.getLogger("mcp_portal")


class _InboundVerifier(TokenVerifier):
    """The `TokenVerifier` the SDK's bearer middleware calls, with JWKS
    resolution deferred to server startup.

    `BearerAuthBackend` needs a verifier when the ASGI app is assembled, which
    is synchronous; discovering `jwks_uri` from the issuer is not. Standing in
    for the real `JwtTokenVerifier` keeps discovery at startup — where a
    misconfigured issuer fails the process rather than turning every request
    into an indistinguishable 401 — without making `build_http_app` async.
    """

    def __init__(self, inbound: InboundAuthConfig) -> None:
        self._inbound = inbound
        self._verifier: JwtTokenVerifier | None = None

    @contextlib.asynccontextmanager
    async def resolved(self) -> AsyncIterator[None]:
        """Discover the JWKS and hold its client open for the server's lifetime."""
        inbound = self._inbound
        # Guaranteed by `InboundAuthConfig._enabled_needs_issuer_and_audience`.
        assert inbound.issuer is not None and inbound.audience is not None
        async with httpx.AsyncClient() as client:
            jwks_uri = inbound.jwks_uri or await discover_jwks_uri(client, inbound.issuer)
            self._verifier = JwtTokenVerifier(
                issuer=inbound.issuer,
                audience=inbound.audience,
                algorithms=inbound.algorithms,
                required_scopes=inbound.required_scopes,
                leeway_s=inbound.leeway_s,
                jwks=JwksCache(client, jwks_uri),
            )
            try:
                yield
            finally:
                self._verifier = None

    async def verify_token(self, token: str) -> AccessToken | None:
        if self._verifier is None:
            # Not a token problem, so not a 401: the server is being asked to
            # authenticate before its lifespan ran.
            raise InboundAuthError("inbound auth is unresolved; the server lifespan has not run")
        return await self._verifier.verify_token(token)


def _security_settings(config: Config) -> TransportSecuritySettings:
    """Host/Origin allow-lists for the SDK's DNS-rebinding defense.

    The Host allow-list has to name how a client *reaches* this server, which
    is not the same thing as the address it *binds* to.

    On a loopback bind the three loopback spellings are interchangeable — a
    client told to use `127.0.0.1` may equally well be pointed at `localhost`
    or `[::1]` — so all three are allowed at any port, matching the `mcp` SDK's
    own default (`Server.streamable_http_app`). Deriving the list from the bind
    address alone would 421 a perfectly ordinary `Host: localhost:8443`, and
    would never match an IPv6 bind at all, since the wire format brackets the
    literal (`[::1]:8443`) while the config does not.

    On a non-loopback bind the bind address is still allowed (bracketed, for an
    IPv6 literal), but it is frequently not what clients send: behind a reverse
    proxy, or bound to `0.0.0.0`, the public hostname differs, so
    `server.http.allowed_hosts` lets the operator name it. When inbound auth is
    enabled, `auth.inbound.audience` is this server's public resource identifier
    (an absolute http(s) URL, validated by
    `InboundAuthConfig._enabled_needs_issuer_and_audience`), so its hostname
    (and port, if given) is allowed too.
    """
    http = config.server.http
    if http.host in LOOPBACK_HOSTS:
        allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    else:
        host = f"[{http.host}]" if ":" in http.host and not http.host.startswith("[") else http.host
        allowed_hosts = [f"{host}:{http.port}", host]
    allowed_hosts += list(http.allowed_hosts)
    inbound = config.auth.inbound
    if inbound.enabled and inbound.audience is not None:
        audience = urlparse(inbound.audience)
        if audience.hostname is not None:
            allowed_hosts.append(audience.hostname)
            if audience.port is not None:
                allowed_hosts.append(f"{audience.hostname}:{audience.port}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=list(http.allowed_origins),
    )


def _auth_settings(inbound: InboundAuthConfig) -> AuthSettings:
    assert inbound.issuer is not None and inbound.audience is not None
    try:
        # Validated through the model rather than by building `AnyHttpUrl`s
        # here: `AuthSettings` preserves an empty URL path, and RFC 8414
        # compares issuers as exact strings, so a spurious trailing slash on a
        # path-less issuer would break discovery.
        return AuthSettings.model_validate(
            {
                "issuer_url": inbound.issuer,
                "resource_server_url": inbound.audience,
                "required_scopes": inbound.required_scopes or None,
                # `JwtTokenVerifier` already rejects a token whose `aud` is not
                # this resource (RFC 9068), so the SDK's own resource check
                # would be a second copy of the same test.
                "validate_token_resource": False,
            }
        )
    except ValidationError as exc:
        raise ConfigError(
            f"auth.inbound.audience {inbound.audience!r} must be an absolute http(s) URL "
            "under transport 'http': it is this server's resource identifier, and RFC 9728 "
            "derives the protected-resource metadata URL from it"
        ) from exc


def _principal_for_request(config: Config) -> Principal | None:
    """Resolve the calling principal for one MCP request, or `None` to deny.

    With inbound auth enabled the SDK's `RequireAuthMiddleware` has already
    rejected anything without a valid token, so `get_access_token()` returns the
    token this request was authorized with; a missing one would mean the auth
    stack is not actually in the request path, which denies rather than falls
    back. With inbound auth disabled (only reachable because the config's
    unauthenticated-HTTP guard passed at load time) the same self-asserted local
    principal as the stdio path applies — a guardrail against an over-eager
    agent, not a security boundary.
    """
    if not config.auth.inbound.enabled:
        return local_principal(config.auth.local_principal)
    access_token = get_access_token()
    if access_token is None:
        return None
    # A malformed `authorization_details` claim denies the request (§8) rather
    # than being read as an absent claim.
    return principal_from_access_token(access_token)


def _build_server(app: App, config: Config, verifier: _InboundVerifier | None) -> Server[None]:
    """The lowlevel server for the HTTP path.

    Deliberately not `stdio.build_server`: the SDK captures the handlers in a
    dispatch table at construction, so the call handler cannot be swapped
    afterwards, and HTTP needs one that resolves a principal per request.
    """

    async def on_list_tools(
        context: object, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=app.invoker.tools())

    async def on_call_tool(
        context: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        principal = _principal_for_request(config)
        if principal is None:
            # §10: the caller learns that it was denied, never why.
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=f"authorization denied for {params.name!r}")
                ],
                is_error=True,
            )
        return await app.invoker.call(params.name, params.arguments, principal=principal)

    @contextlib.asynccontextmanager
    async def lifespan(server: Server[None]) -> AsyncIterator[None]:
        if verifier is None:
            yield None
            return
        async with verifier.resolved():
            yield None

    return Server(
        name=config.server.name,
        version="0.1.0",
        lifespan=lifespan,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def build_http_app(app: App, config: Config, secrets: Mapping[str, str]) -> Starlette:
    """Assemble the ASGI app serving `app` over streamable HTTP.

    `secrets` is part of the transport's signature because a transport is
    constructed from a loaded config; inbound auth needs none of them today
    (the IdP is contacted anonymously for its JWKS).
    """
    http = config.server.http
    inbound = config.auth.inbound
    verifier = _InboundVerifier(inbound) if inbound.enabled else None
    if verifier is None:
        names = sorted(tool.name for tool in app.invoker.tools())
        log.warning(
            "transport 'http' with auth.inbound.enabled=false on %s: the local principal "
            "applies to every request for %d tool(s): %s. This is a guardrail against an "
            "over-eager agent, not a security boundary — anyone who can reach this endpoint "
            "can call the upstream directly.",
            f"{http.host}:{http.port}",
            len(names),
            ", ".join(names) or "(none)",
        )

    server = _build_server(app, config, verifier)
    return server.streamable_http_app(
        streamable_http_path=http.path,
        # Sessions are on: `Mcp-Session-Id` is issued and `GET` opens an SSE
        # channel, which several MCP clients expect after `initialize`. A
        # session id alone is never a credential — every request is still
        # independently bearer-verified — and the SDK's own
        # `StreamableHTTPSessionManager` already binds each session to the
        # `AuthorizationContext` (subject/client_id/issuer) of whoever
        # created it, rejecting a mismatched reuse with 404 before this
        # server ever sees the request — when a bearer principal exists
        # (`auth.inbound.enabled=true`); without one there is no
        # `AuthorizationContext` to bind to.
        stateless_http=False,
        session_idle_timeout=(
            http.session_idle_timeout_s
            if http.session_idle_timeout_s is not None
            else DEFAULT_SESSION_IDLE_TIMEOUT
        ),
        max_sessions=http.max_sessions if http.max_sessions is not None else DEFAULT_MAX_SESSIONS,
        transport_security=_security_settings(config),
        auth=_auth_settings(inbound) if verifier is not None else None,
        token_verifier=verifier,
    )
