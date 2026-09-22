"""End-to-end: the full broker over streamable HTTP.

Mirrors `tests/test_p3_end_to_end.py` (stdio + local-principal RAR), but for
the HTTP path: a caller-supplied bearer JWT is verified against a fake IdP,
RAR-policy-checked against the JWT's own `authorization_details` claim, and —
on success — exchanged (RFC 8693) for an upstream token that carries only the
policy's *required* detail, not the caller's broader presented one. The
exchanged token, not the caller's bearer token, is what the fake upstream
sees.

This proves Tasks 1-8 compose correctly; it adds no new production code.
"""

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from ruamel.yaml import YAML

from mcp_portal.app import App, build_app
from mcp_portal.config.loader import load_config
from mcp_portal.server.http import build_http_app

_yaml = YAML()

BASE_URL = "http://127.0.0.1:8443"

REQUIRED_DETAIL = {
    "type": "payment_initiation",
    "actions": ["initiate"],
    "locations": ["https://api.example.com/v1/payments"],
}

# What the caller's own token presents: a strictly broader grant than the
# policy requires, so a naive implementation that carried the *presented*
# set (rather than the accumulated *required* one) would be distinguishable
# from the correct behavior in assertion (4) below.
PRESENTED_DETAIL = {
    "type": "payment_initiation",
    "actions": ["initiate", "refund"],
    "locations": [
        "https://api.example.com/v1/payments",
        "https://api.example.com/v1/refunds",
    ],
}


@pytest.fixture
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwk(rsa_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True)
    return dict(public_jwk) | {"kid": "test-kid", "use": "sig", "alg": "RS256"}


def _bearer(rsa_key: rsa.RSAPrivateKey, authorization_details: list[dict] | None = None) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "https://idp.example.com",
        "aud": "https://api.example.com/mcp",
        "sub": "user-123",
        "exp": now + 300,
        "scope": "invoices.read",
    }
    if authorization_details is not None:
        claims["authorization_details"] = authorization_details
    return jwt.encode(
        claims, rsa_key, algorithm="RS256", headers={"kid": "test-kid", "typ": "at+jwt"}
    )


CONFIG: dict = {
    "version": "1",
    "mode": "configured",
    "server": {
        "name": "billing-portal",
        "transport": "http",
        "http": {"allowed_origins": ["https://client.example.com"]},
    },
    "upstreams": {
        "billing": {
            "base_url": "https://api.example.com",
            "auth": {
                "outbound": {
                    "mode": "token_exchange",
                    "token_endpoint": "https://idp.example.com/oauth2/token",
                    "client_id": "sidekit-billing",
                    "client_secret": "${env:BILLING_TOKEN_EXCHANGE_SECRET}",
                    "audience": "https://api.example.com",
                }
            },
        }
    },
    "operations": [
        {
            "id": "initiate_payment",
            "upstream": "billing",
            "description": "Initiate a payment.",
            "group_tags": ["billing"],
            "binding": {
                "method": "POST",
                "path": "/v1/payments",
                "body": {
                    "content_type": "application/json",
                    "schema": {"type": "object", "properties": {"amount": {"type": "integer"}}},
                },
            },
        }
    ],
    "auth": {
        "inbound": {
            "enabled": True,
            "issuer": "https://idp.example.com",
            "audience": "https://api.example.com/mcp",
        }
    },
    "policy": {"file": "./p4-policy.yaml"},
}

POLICY: dict = {
    "version": "1",
    "defaults": {"unmatched": "allow"},
    "rules": [
        {
            "match": {"tags": ["billing"], "effect": ["action"]},
            "require": {"authorization_details": [REQUIRED_DETAIL]},
            "outbound": {"carry": True},
        }
    ],
}


def _idp_and_upstream_handler(
    jwk: dict[str, Any],
    token_requests: list[httpx.Request],
    upstream_requests: list[httpx.Request],
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"jwks_uri": "https://idp.example.com/jwks.json"})
        if request.url.path == "/jwks.json":
            return httpx.Response(200, json={"keys": [jwk]})
        if request.url.path == "/oauth2/token":
            token_requests.append(request)
            return httpx.Response(200, json={"access_token": "exchanged-tok", "expires_in": 900})
        upstream_requests.append(request)
        return httpx.Response(200, json={"ok": True})

    return handler


@asynccontextmanager
async def _client_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("BILLING_TOKEN_EXCHANGE_SECRET", "client-secret-value")

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(CONFIG))
    policy_path = tmp_path / "p4-policy.yaml"
    with policy_path.open("w") as f:
        _yaml.dump(POLICY, f)

    loaded = load_config(config_path)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    app: App = build_app(loaded)
    asgi_app = build_http_app(app, loaded.config, loaded.secrets)
    try:
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
async def test_a_call_without_the_required_claim_is_denied_before_the_upstream_is_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey, jwk: dict[str, Any]
) -> None:
    token_requests: list[httpx.Request] = []
    upstream_requests: list[httpx.Request] = []
    async with _client_for(
        tmp_path, monkeypatch, _idp_and_upstream_handler(jwk, token_requests, upstream_requests)
    ) as client:
        token = _bearer(rsa_key)  # no authorization_details claim at all
        response = await client.post(
            "/mcp",
            json=_rpc("tools/call", name="initiate_payment", arguments={"amount": 100}),
            headers=MCP_HEADERS | {"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert '"isError":true' in response.text.replace(" ", "")
    assert "payment_initiation" in response.text
    # (1) Denied before the upstream was ever reached: no token exchange, no
    # upstream call happened.
    assert token_requests == []
    assert upstream_requests == []


@pytest.mark.anyio
async def test_a_call_with_the_claim_succeeds_and_carries_the_exchanged_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey, jwk: dict[str, Any]
) -> None:
    token_requests: list[httpx.Request] = []
    upstream_requests: list[httpx.Request] = []
    async with _client_for(
        tmp_path, monkeypatch, _idp_and_upstream_handler(jwk, token_requests, upstream_requests)
    ) as client:
        caller_token = _bearer(rsa_key, authorization_details=[PRESENTED_DETAIL])
        response = await client.post(
            "/mcp",
            json=_rpc("tools/call", name="initiate_payment", arguments={"amount": 100}),
            headers=MCP_HEADERS | {"Authorization": f"Bearer {caller_token}"},
        )

    # (2) The call with the claim succeeds.
    assert response.status_code == 200
    assert '"isError":true' not in response.text.replace(" ", "")
    assert "ok" in response.text

    # (3) The fake upstream received the *exchanged* token, not the caller's
    # own bearer token.
    (upstream_request,) = upstream_requests
    assert upstream_request.headers["authorization"] == "Bearer exchanged-tok"
    assert upstream_request.headers["authorization"] != f"Bearer {caller_token}"

    # (4) The fake token endpoint's request body carried the accumulated
    # *required* detail, not the caller's broader presented set.
    (token_request,) = token_requests
    body = token_request.read().decode()
    assert f"subject_token={caller_token}" in body
    form = parse_qs(body)
    carried = json.loads(form["authorization_details"][0])
    assert carried == [REQUIRED_DETAIL]
    assert carried != [PRESENTED_DETAIL]
