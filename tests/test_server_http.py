"""The streamable HTTP transport, end to end over ASGI.

Every request here goes through the real `mcp` SDK app the gateway assembles
(`Server.streamable_http_app`), so the Origin/Host defense, the 401 shape and
the RFC 9728 metadata route are exercised as the SDK actually implements them,
not as this project imagines them.
"""

import json
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_portal.app import build_app
from mcp_portal.auth.principal import Principal
from mcp_portal.config.loader import load_config
from mcp_portal.server.http import build_http_app

# The config's own `server.http` host/port: the SDK validates the Host header
# against it, so the test client must address the gateway the way a real client
# would rather than through an arbitrary base URL.
BASE_URL = "http://127.0.0.1:8443"

INBOUND = {
    "inbound": {
        "enabled": True,
        "issuer": "https://idp.example.com",
        "audience": "https://api.example.com/mcp",
    }
}


@pytest.fixture
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwk(rsa_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True)
    return dict(public_jwk) | {"kid": "test-kid", "use": "sig", "alg": "RS256"}


def _bearer(rsa_key: rsa.RSAPrivateKey, **claim_overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "https://idp.example.com",
        "aud": "https://api.example.com/mcp",
        "sub": "user-123",
        "exp": now + 300,
        "scope": "invoices.read",
    } | claim_overrides
    return jwt.encode(
        claims, rsa_key, algorithm="RS256", headers={"kid": "test-kid", "typ": "at+jwt"}
    )


def _config(**overrides: Any) -> dict[str, Any]:
    return {
        "version": "1",
        "mode": "configured",
        "server": {
            "name": "s",
            "transport": "http",
            "http": {"allowed_origins": ["https://client.example.com"]},
        },
        "upstreams": {"billing": {"base_url": "https://api.example.com"}},
        "operations": [
            {
                "id": "list_invoices",
                "upstream": "billing",
                "description": "List invoices.",
                "binding": {"method": "GET", "path": "/v1/invoices"},
            }
        ],
    } | overrides


@asynccontextmanager
async def _client_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, Any],
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    principals: list[Principal | None] | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Serve `config` over ASGI, optionally answering every outgoing HTTP
    request (JWKS discovery and upstream calls alike) with `handler`."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    loaded = load_config(path)

    real_client = httpx.AsyncClient
    if handler is not None:
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
        )

    app = build_app(loaded)

    if principals is not None:
        inner = app.invoker.call

        async def recording_call(
            name: str,
            arguments: Mapping[str, Any] | None,
            principal: Principal | None = None,
        ) -> Any:
            principals.append(principal)
            return await inner(name, arguments, principal=principal)

        monkeypatch.setattr(app.invoker, "call", recording_call)

    asgi_app = build_http_app(app, loaded.config, loaded.secrets)
    try:
        # The session manager (and, with inbound auth on, JWKS discovery) runs
        # in the app's lifespan, which `ASGITransport` does not drive itself.
        async with (
            asgi_app.router.lifespan_context(asgi_app),
            real_client(transport=httpx.ASGITransport(app=asgi_app), base_url=BASE_URL) as client,
        ):
            yield client
    finally:
        await app.aclose()


def _rpc(method: str, **params: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


MCP_HEADERS = {
    "Origin": "https://client.example.com",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.mark.anyio
async def test_a_mismatched_origin_is_rejected_with_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _client_for(tmp_path, monkeypatch, _config()) as client:
        response = await client.post(
            "/mcp",
            json=_rpc("tools/list"),
            headers=MCP_HEADERS | {"Origin": "https://evil.example.com"},
        )
    assert response.status_code == 403
    # Specifically the SDK's DNS-rebinding defense, not some other refusal:
    # the same request with an allowed Origin is served (see the local-principal
    # test below).
    assert response.text == "Invalid Origin header"


@pytest.mark.anyio
async def test_no_bearer_token_is_a_401_with_www_authenticate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jwk: dict[str, Any]
) -> None:
    async with _client_for(
        tmp_path, monkeypatch, _config(auth=INBOUND), handler=_idp_handler(jwk)
    ) as client:
        response = await client.post("/mcp", json=_rpc("tools/list"), headers=MCP_HEADERS)
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp" in response.headers["www-authenticate"]


@pytest.mark.anyio
async def test_an_unverifiable_bearer_token_is_a_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jwk: dict[str, Any]
) -> None:
    """The token is a well-formed JWT signed by a key the JWKS does not have —
    only `JwtTokenVerifier` can tell, so this proves the verifier is wired in."""
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with _client_for(
        tmp_path, monkeypatch, _config(auth=INBOUND), handler=_idp_handler(jwk)
    ) as client:
        response = await client.post(
            "/mcp",
            json=_rpc("tools/list"),
            headers=MCP_HEADERS | {"Authorization": f"Bearer {_bearer(other_key)}"},
        )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_the_protected_resource_metadata_route_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jwk: dict[str, Any]
) -> None:
    async with _client_for(
        tmp_path, monkeypatch, _config(auth=INBOUND), handler=_idp_handler(jwk)
    ) as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "https://api.example.com/mcp"
    assert body["authorization_servers"] == ["https://idp.example.com"]


def _idp_handler(jwk: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer issuer discovery and JWKS; anything else is the billing upstream."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"jwks_uri": "https://idp.example.com/jwks.json"})
        if request.url.path == "/jwks.json":
            return httpx.Response(200, json={"keys": [jwk]})
        return httpx.Response(200, json={"invoices": []})

    return handler


@pytest.mark.anyio
async def test_a_valid_bearer_token_reaches_the_tool_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rsa_key: rsa.RSAPrivateKey,
    jwk: dict[str, Any],
) -> None:
    principals: list[Principal | None] = []
    async with _client_for(
        tmp_path,
        monkeypatch,
        _config(auth=INBOUND),
        handler=_idp_handler(jwk),
        principals=principals,
    ) as client:
        token = _bearer(rsa_key)
        auth_headers = MCP_HEADERS | {"Authorization": f"Bearer {token}"}
        session_id = await _initialize(client, auth_headers)
        response = await client.post(
            "/mcp",
            json=_rpc("tools/call", name="list_invoices", arguments={}),
            headers=auth_headers | {"Mcp-Session-Id": session_id},
        )

    assert response.status_code == 200
    assert "invoices" in response.text
    assert '"isError":true' not in response.text.replace(" ", "")
    # The call ran as the token's subject, carrying the token itself for a
    # later RFC 8693 exchange — not as the local principal.
    assert [p and p.identity for p in principals] == ["user-123"]
    assert principals[0] is not None and principals[0].subject_token == token


@pytest.mark.anyio
async def test_a_host_header_matching_the_audience_is_not_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rsa_key: rsa.RSAPrivateKey,
    jwk: dict[str, Any],
) -> None:
    """`auth.inbound.audience` names the public host clients actually reach the
    server through (`https://api.example.com/mcp`), which is unrelated to the
    loopback bind address (`server.http.host`, defaulted to `127.0.0.1`). A
    request whose `Host` header names the audience's hostname must be allowed
    through the SDK's DNS-rebinding defense, not rejected with a 421 — proving
    the allow-list is built from the public resource identifier, not just the
    bind address."""
    async with _client_for(
        tmp_path, monkeypatch, _config(auth=INBOUND), handler=_idp_handler(jwk)
    ) as client:
        token = _bearer(rsa_key)
        headers = MCP_HEADERS | {"Authorization": f"Bearer {token}", "Host": "api.example.com"}
        session_id = await _initialize(client, headers)
        response = await client.post(
            "/mcp",
            json=_rpc("tools/call", name="list_invoices", arguments={}),
            headers=headers | {"Mcp-Session-Id": session_id},
        )

    assert response.status_code != 421
    assert response.status_code == 200
    assert "invoices" in response.text


@pytest.mark.anyio
async def test_localhost_is_accepted_under_the_default_loopback_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jwk: dict[str, Any]
) -> None:
    """`http://localhost:8443/mcp` is as valid and as documented a way to reach a
    server bound to `127.0.0.1` as the dotted-quad is, and it puts
    `Host: localhost:8443` on the wire. Deriving the allow-list from the bind
    address alone would 421 that on a fresh install."""
    async with _client_for(tmp_path, monkeypatch, _config(), handler=_idp_handler(jwk)) as client:
        headers = MCP_HEADERS | {"Host": "localhost:8443"}
        session_id = await _initialize(client, headers)
        response = await client.post(
            "/mcp",
            json=_rpc("tools/call", name="list_invoices", arguments={}),
            headers=headers | {"Mcp-Session-Id": session_id},
        )

    assert response.status_code != 421
    assert response.status_code == 200
    assert "invoices" in response.text


@pytest.mark.anyio
async def test_a_bracketed_ipv6_loopback_host_header_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jwk: dict[str, Any]
) -> None:
    """An IPv6 literal is written unbracketed in config (`host: "::1"`) but
    bracketed on the wire (`Host: [::1]:8443`). Copying the config spelling into
    the allow-list would never match, 421ing every request."""
    config = _config(
        server={
            "name": "s",
            "transport": "http",
            "http": {"host": "::1", "allowed_origins": ["https://client.example.com"]},
        }
    )
    async with _client_for(tmp_path, monkeypatch, config, handler=_idp_handler(jwk)) as client:
        headers = MCP_HEADERS | {"Host": "[::1]:8443"}
        session_id = await _initialize(client, headers)
        response = await client.post(
            "/mcp",
            json=_rpc("tools/call", name="list_invoices", arguments={}),
            headers=headers | {"Mcp-Session-Id": session_id},
        )

    assert response.status_code != 421
    assert response.status_code == 200
    assert "invoices" in response.text


@pytest.mark.anyio
async def test_a_configured_allowed_host_is_accepted_on_a_wildcard_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jwk: dict[str, Any]
) -> None:
    """Binding `0.0.0.0` with `allow_unauthenticated_http` is a combination the
    config validators explicitly permit, but no client ever sends
    `Host: 0.0.0.0` — without `server.http.allowed_hosts` the operator has no way
    to name the host their clients actually use, and the sanctioned escape hatch
    421s every request."""
    config = _config(
        server={
            "name": "s",
            "transport": "http",
            "http": {
                "host": "0.0.0.0",
                "allowed_origins": ["https://client.example.com"],
                "allowed_hosts": ["gateway.example.com"],
            },
        },
        auth={"inbound": {"allow_unauthenticated_http": True}},
    )
    async with _client_for(tmp_path, monkeypatch, config, handler=_idp_handler(jwk)) as client:
        headers = MCP_HEADERS | {"Host": "gateway.example.com"}
        session_id = await _initialize(client, headers)
        response = await client.post(
            "/mcp",
            json=_rpc("tools/call", name="list_invoices", arguments={}),
            headers=headers | {"Mcp-Session-Id": session_id},
        )

    assert response.status_code != 421
    assert response.status_code == 200
    assert "invoices" in response.text


@pytest.mark.anyio
async def test_inbound_disabled_on_loopback_uses_the_local_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jwk: dict[str, Any]
) -> None:
    principals: list[Principal | None] = []
    async with _client_for(
        tmp_path,
        monkeypatch,
        _config(),
        handler=_idp_handler(jwk),
        principals=principals,
    ) as client:
        session_id = await _initialize(client, MCP_HEADERS)
        response = await client.post(
            "/mcp",
            json=_rpc("tools/call", name="list_invoices", arguments={}),
            headers=MCP_HEADERS | {"Mcp-Session-Id": session_id},
        )

    assert response.status_code == 200
    assert "invoices" in response.text
    assert [p and p.identity for p in principals] == ["local"]


async def _initialize(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    """Perform the MCP `initialize` handshake and return the issued `Mcp-Session-Id`."""
    response = await client.post(
        "/mcp",
        json=_rpc(
            "initialize",
            protocolVersion="2025-06-18",
            capabilities={},
            clientInfo={"name": "test-client", "version": "0.0.1"},
        ),
        headers=headers,
    )
    assert response.status_code == 200, response.text
    session_id = response.headers["mcp-session-id"]
    assert session_id
    return session_id


@pytest.mark.anyio
async def test_initialize_issues_a_session_id_reusable_on_a_later_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jwk: dict[str, Any]
) -> None:
    async with _client_for(tmp_path, monkeypatch, _config(), handler=_idp_handler(jwk)) as client:
        session_id = await _initialize(client, MCP_HEADERS)
        response = await client.post(
            "/mcp",
            json=_rpc("tools/call", name="list_invoices", arguments={}),
            headers=MCP_HEADERS | {"Mcp-Session-Id": session_id},
        )
    assert response.status_code == 200
    assert "invoices" in response.text
