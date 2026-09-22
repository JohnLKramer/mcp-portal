import asyncio
import random
import time

import httpx
import pytest

from mcp_portal.config.models import UpstreamConfig
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity
from mcp_portal.transports.http import Credential, HttpTransport, StaticCredentialSource


def op(effect: Effect = Effect.READ_ONLY, method: str = "GET") -> Operation:
    return Operation(
        id="x",
        upstream="billing",
        name="x",
        title="x",
        description="d",
        group_tags=(),
        effect=effect,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method=method, path="/v1/x"),
    )


def transport(handler, credential: Credential | None = None, **upstream_kw) -> HttpTransport:
    upstream = UpstreamConfig(base_url="https://api.example.com", **upstream_kw)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpTransport(
        client=client, upstream=upstream, credential_source=StaticCredentialSource(credential)
    )


@pytest.mark.anyio
async def test_successful_response_is_returned_as_text():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    result = await transport(handler).execute(op(), {})
    assert result.status == 200
    assert "ok" in result.text
    assert result.truncated is False


@pytest.mark.anyio
async def test_response_is_capped_and_marked_truncated():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 5000)

    result = await transport(handler, max_response_bytes=100).execute(op(), {})
    assert result.truncated is True
    assert result.original_bytes == 5000
    assert len(result.text.encode()) <= 200  # cap plus the truncation marker


@pytest.mark.anyio
async def test_binary_content_is_described_not_inlined():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 500,
            headers={"content-type": "image/png"},
        )

    result = await transport(handler).execute(op(), {})
    assert "image/png" in result.text
    assert "508" in result.text
    assert "PNG" not in result.text


@pytest.mark.anyio
async def test_plain_text_is_passed_through():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hello", headers={"content-type": "text/plain"})

    assert (await transport(handler).execute(op(), {})).text == "hello"


@pytest.mark.anyio
async def test_read_only_operation_retries_a_503():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, text="ok")

    result = await transport(handler).execute(op(Effect.READ_ONLY), {})
    assert calls == 2
    assert result.status == 200


@pytest.mark.anyio
async def test_action_operation_is_never_retried():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="down")

    result = await transport(handler).execute(op(Effect.ACTION, "POST"), {})
    assert calls == 1
    assert result.status == 503


@pytest.mark.anyio
async def test_4xx_is_not_retried():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="nope")

    result = await transport(handler).execute(op(), {})
    assert calls == 1
    assert result.status == 404


@pytest.mark.anyio
async def test_retries_are_bounded_at_three_attempts():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="bad gateway")

    result = await transport(handler).execute(op(), {})
    assert calls == 3
    assert result.status == 502


@pytest.mark.anyio
async def test_the_total_time_budget_stops_retrying_before_the_attempt_budget():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(503, text="down")

    # The per-attempt timeout leaves room for all three attempts; the total budget
    # is spent by the end of the first one.
    result = await transport(handler, timeout_ms=5000, max_total_ms=10).execute(op(), {})
    assert calls == 1
    assert result.status == 503


@pytest.mark.anyio
async def test_the_total_time_budget_stops_retrying_a_timeout():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        raise httpx.ReadTimeout("upstream took too long")

    with pytest.raises(httpx.ReadTimeout):
        await transport(handler, timeout_ms=5000, max_total_ms=10).execute(op(), {})
    assert calls == 1


@pytest.mark.anyio
async def test_a_generous_total_budget_leaves_the_attempt_budget_intact():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="bad gateway")

    await transport(handler, timeout_ms=5000, max_total_ms=60000).execute(op(), {})
    assert calls == 3


@pytest.mark.anyio
async def test_the_total_time_budget_caps_the_elapsed_backoff_sequence(
    monkeypatch: pytest.MonkeyPatch,
):
    # Backoff is full jitter (random.uniform(0, backoff)); pin it to its max so
    # the gate's decision doesn't depend on which delay a given run happens to
    # draw. Without this, a small first delay can leave enough budget for a
    # third attempt and the test flakes (~10% of runs).
    monkeypatch.setattr(random, "uniform", lambda _low, high: high)

    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.04)
        return httpx.Response(503, text="down")

    # Three attempts plus backoff would run past 250ms, so the loop has to stop
    # one attempt short rather than noticing the overrun after the fact.
    started = time.monotonic()
    result = await transport(handler, timeout_ms=100, max_total_ms=250).execute(op(), {})
    elapsed = time.monotonic() - started

    assert calls == 2
    assert result.status == 503
    assert elapsed < 0.35  # max_total_ms + one timeout_ms, the tolerated overshoot


@pytest.mark.anyio
async def test_a_retry_after_longer_than_the_total_budget_is_not_slept_off():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="slow down", headers={"retry-after": "3"})

    started = time.monotonic()
    result = await transport(handler, timeout_ms=50, max_total_ms=100).execute(op(), {})
    elapsed = time.monotonic() - started

    assert calls == 1
    assert result.status == 429
    assert elapsed < 0.5  # not the 3s the header asked for


@pytest.mark.anyio
async def test_a_retry_after_inside_the_total_budget_is_still_honoured():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="slow down", headers={"retry-after": "1"})
        return httpx.Response(200, text="ok")

    started = time.monotonic()
    result = await transport(handler, timeout_ms=1000, max_total_ms=5000).execute(op(), {})
    elapsed = time.monotonic() - started

    assert calls == 2
    assert result.status == 200
    assert elapsed >= 1.0


@pytest.mark.anyio
async def test_the_credential_header_reaches_the_outbound_request():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    credential = Credential(header="Authorization", value="Bearer sk-test")
    await transport(handler, credential=credential).execute(op(), {})
    assert seen[0].headers["authorization"] == "Bearer sk-test"
