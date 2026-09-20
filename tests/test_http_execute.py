import httpx
import pytest

from mcp_portal.config.models import UpstreamConfig
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity
from mcp_portal.transports.http import HttpTransport


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


def transport(handler, **upstream_kw) -> HttpTransport:
    upstream = UpstreamConfig(base_url="https://api.example.com", **upstream_kw)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpTransport(client=client, upstream=upstream, credential=None)


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
