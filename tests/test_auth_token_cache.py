import asyncio

import pytest

from mcp_portal.auth.token_cache import TokenCache


@pytest.mark.anyio
async def test_a_fresh_key_calls_fetch_once():
    calls = []

    async def fetch() -> tuple[str, float]:
        calls.append(1)
        return "token-1", 60.0

    cache = TokenCache()
    assert await cache.get_or_fetch("k", fetch) == "token-1"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_a_cached_key_within_ttl_does_not_refetch():
    calls = []

    async def fetch() -> tuple[str, float]:
        calls.append(1)
        return f"token-{len(calls)}", 60.0

    cache = TokenCache()
    await cache.get_or_fetch("k", fetch)
    assert await cache.get_or_fetch("k", fetch) == "token-1"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_an_expired_key_refetches():
    calls = []

    async def fetch() -> tuple[str, float]:
        calls.append(1)
        return f"token-{len(calls)}", 0.0  # expires immediately

    cache = TokenCache()
    await cache.get_or_fetch("k", fetch)
    await asyncio.sleep(0)
    assert await cache.get_or_fetch("k", fetch) == "token-2"
    assert len(calls) == 2


@pytest.mark.anyio
async def test_distinct_keys_do_not_share_an_entry():
    async def fetch_a() -> tuple[str, float]:
        return "a", 60.0

    async def fetch_b() -> tuple[str, float]:
        return "b", 60.0

    cache = TokenCache()
    assert await cache.get_or_fetch("a", fetch_a) == "a"
    assert await cache.get_or_fetch("b", fetch_b) == "b"


@pytest.mark.anyio
async def test_concurrent_callers_for_the_same_key_single_flight_the_fetch():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetch() -> tuple[str, float]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "token", 60.0

    cache = TokenCache()
    first = asyncio.ensure_future(cache.get_or_fetch("k", fetch))
    await started.wait()
    second = asyncio.ensure_future(cache.get_or_fetch("k", fetch))
    await asyncio.sleep(0.01)  # let `second` reach and block on the same lock
    release.set()

    assert await first == "token"
    assert await second == "token"
    assert calls == 1
