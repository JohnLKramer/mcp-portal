import httpx
import pytest

from mcp_portal.auth.outbound import ClientCredentialsSource, OutboundError
from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.auth.token_cache import TokenCache
from mcp_portal.config.models import OutboundConfig


def cfg(**overrides) -> OutboundConfig:
    base = {
        "mode": "client_credentials",
        "token_endpoint": "https://idp.example.com/oauth2/token",
        "client_id": "sidekit-billing",
        "client_secret": "${env:SECRET}",
        "scopes": ["invoices.write"],
    }
    return OutboundConfig(**(base | overrides))


def source(handler, **cfg_overrides) -> ClientCredentialsSource:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ClientCredentialsSource(
        outbound=cfg(**cfg_overrides),
        secrets={"${env:SECRET}": "client-secret-value"},
        client=client,
        cache=TokenCache(),
        upstream_key="billing",
    )


@pytest.mark.anyio
async def test_a_successful_token_response_yields_a_bearer_credential():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        body = request.read().decode()
        assert "grant_type=client_credentials" in body
        assert "scope=invoices.write" in body
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    cred = await source(handler).get(carry=())
    assert cred is not None
    assert cred.header == "Authorization"
    assert cred.value == "Bearer tok-1"


@pytest.mark.anyio
async def test_carried_details_are_sent_as_authorization_details():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs

        seen["body"] = parse_qs(request.read().decode())
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    detail = AuthorizationDetail(type="payment_initiation", actions=("initiate",))
    await source(handler).get(carry=(detail,))
    import json as _json

    sent = _json.loads(seen["body"]["authorization_details"][0])
    assert sent == [{"type": "payment_initiation", "actions": ["initiate"]}]


@pytest.mark.anyio
async def test_a_non_200_token_response_is_an_outbound_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client"})

    with pytest.raises(OutboundError):
        await source(handler).get(carry=())


@pytest.mark.anyio
async def test_a_response_missing_access_token_is_an_outbound_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"expires_in": 3600})

    with pytest.raises(OutboundError):
        await source(handler).get(carry=())


@pytest.mark.anyio
async def test_two_calls_within_ttl_reuse_the_cached_token():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": f"tok-{len(calls)}", "expires_in": 3600})

    src = source(handler)
    first = await src.get(carry=())
    second = await src.get(carry=())
    assert first.value == second.value
    assert len(calls) == 1
