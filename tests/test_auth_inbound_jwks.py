import time

import httpx
import pytest

from mcp_portal.auth.inbound import InboundAuthError, JwksCache, discover_jwks_uri

JWKS_ONE_KEY = {"keys": [{"kid": "k1", "kty": "RSA", "n": "x", "e": "AQAB"}]}


@pytest.mark.anyio
async def test_key_for_a_known_kid_is_returned_without_a_fetch():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=JWKS_ONE_KEY)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(client, "https://idp.example.com/jwks.json")
    assert (await cache.key_for("k1"))["kid"] == "k1"
    assert (await cache.key_for("k1"))["kid"] == "k1"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_an_unknown_kid_triggers_exactly_one_refresh():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=JWKS_ONE_KEY)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(client, "https://idp.example.com/jwks.json")
    assert await cache.key_for("ghost") is None
    assert len(calls) == 1


@pytest.mark.anyio
async def test_refresh_is_rate_limited_to_once_per_60_seconds(monkeypatch):
    calls = []
    now = [1000.0]

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=JWKS_ONE_KEY)

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(client, "https://idp.example.com/jwks.json")

    await cache.key_for("ghost-1")
    await cache.key_for("ghost-2")  # still unknown, but within the 60s window
    assert len(calls) == 1

    now[0] += 61
    await cache.key_for("ghost-3")
    assert len(calls) == 2


@pytest.mark.anyio
async def test_a_failing_jwks_endpoint_denies_rather_than_raising():
    """A down or flaky IdP must look like an unknown `kid` — which the verifier
    turns into a clean 401 — not like an escaping `httpx.HTTPError`, which
    Starlette's auth middleware does not catch and which would surface as a 500."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(client, "https://idp.example.com/jwks.json")
    assert await cache.key_for("k1") is None


@pytest.mark.anyio
async def test_a_failing_jwks_endpoint_leaves_already_cached_keys_usable():
    responses = [httpx.Response(200, json=JWKS_ONE_KEY), httpx.Response(503, json={})]
    now = [1000.0]

    async def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(client, "https://idp.example.com/jwks.json")
    assert (await cache.key_for("k1"))["kid"] == "k1"

    # A later unknown kid drives a refresh that fails; the key fetched before
    # the outage must survive it.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(time, "monotonic", lambda: now[0] + 1000)
        assert await cache.key_for("ghost") is None
    assert (await cache.key_for("k1"))["kid"] == "k1"


@pytest.mark.anyio
async def test_a_process_started_near_boot_still_refreshes_on_the_first_call(monkeypatch):
    """`time.monotonic` is boot-relative on Linux/macOS, so a process started a
    few seconds after boot sees a `now` smaller than the refresh interval. The
    first refresh must not be rate-limited away against a zero-initialized
    timestamp — that would reject every token as an unknown `kid` until the
    boot clock passed 60s."""
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=JWKS_ONE_KEY)

    monkeypatch.setattr(time, "monotonic", lambda: 5.0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(client, "https://idp.example.com/jwks.json")

    assert (await cache.key_for("k1"))["kid"] == "k1"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_discover_jwks_uri_prefers_oauth_authorization_server_metadata():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"jwks_uri": "https://idp.example.com/jwks.json"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert (
        await discover_jwks_uri(client, "https://idp.example.com")
        == "https://idp.example.com/jwks.json"
    )


@pytest.mark.anyio
async def test_discover_jwks_uri_falls_back_to_openid_configuration():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"jwks_uri": "https://idp.example.com/jwks.json"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert (
        await discover_jwks_uri(client, "https://idp.example.com")
        == "https://idp.example.com/jwks.json"
    )


@pytest.mark.anyio
async def test_discover_jwks_uri_raises_when_neither_document_is_available():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(InboundAuthError):
        await discover_jwks_uri(client, "https://idp.example.com")
