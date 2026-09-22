import httpx
import pytest

from mcp_portal.auth.outbound import OutboundError, TokenExchangeSource
from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.auth.token_cache import TokenCache


def source(handler, **overrides) -> TokenExchangeSource:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    base = dict(
        token_endpoint="https://idp.example.com/oauth2/token",
        client_id="sidekit-billing",
        client_secret="client-secret-value",
        audience="https://api.example.com",
        requested_token_type="urn:ietf:params:oauth:token-type:access_token",
        scopes=["invoices.write"],
        client=client,
        cache=TokenCache(),
        upstream_key="billing",
    )
    return TokenExchangeSource(**(base | overrides))


@pytest.mark.anyio
async def test_a_successful_exchange_yields_a_bearer_credential():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange" in body
        assert "subject_token=inbound-jwt" in body
        assert "subject_token_type=urn%3Aietf%3Aparams%3Aoauth%3Atoken-type%3Aaccess_token" in body
        assert "audience=https%3A%2F%2Fapi.example.com" in body
        return httpx.Response(200, json={"access_token": "exchanged-tok", "expires_in": 900})

    cred = await source(handler).get(carry=(), subject_token="inbound-jwt")
    assert cred is not None
    assert cred.value == "Bearer exchanged-tok"


@pytest.mark.anyio
async def test_no_subject_token_is_an_outbound_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the token endpoint without a subject token")

    with pytest.raises(OutboundError, match="subject_token"):
        await source(handler).get(carry=(), subject_token=None)


@pytest.mark.anyio
async def test_two_distinct_subject_tokens_do_not_share_a_cache_entry():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": f"tok-{len(calls)}", "expires_in": 900})

    src = source(handler)
    first = await src.get(carry=(), subject_token="token-a")
    second = await src.get(carry=(), subject_token="token-b")
    assert first.value != second.value
    assert len(calls) == 2


@pytest.mark.anyio
async def test_the_same_subject_token_reuses_the_cached_exchanged_token():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": f"tok-{len(calls)}", "expires_in": 900})

    src = source(handler)
    first = await src.get(carry=(), subject_token="token-a")
    second = await src.get(carry=(), subject_token="token-a")
    assert first.value == second.value
    assert len(calls) == 1


@pytest.mark.anyio
async def test_carried_details_are_included_in_the_cache_key_and_the_request():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert "authorization_details" in body
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})

    detail = AuthorizationDetail(type="payment_initiation")
    await source(handler).get(carry=(detail,), subject_token="token-a")
