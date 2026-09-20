"""End-to-end MCP round trips over the SDK's in-memory transport.

These drive the same Server the stdio entrypoint builds, through a real
ClientSession, so the JSON-RPC framing, initialization handshake and result
serialization are exercised rather than assumed.
"""

import json
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp_portal.app import App, build_app
from mcp_portal.config.loader import load_config
from mcp_portal.server.stdio import build_server

CONFIG: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "billing-portal", "transport": "stdio"},
    "upstreams": {
        "billing": {
            "base_url": "https://api.example.com",
            "auth": {"outbound": {"mode": "static", "value": "${env:BILLING_KEY}"}},
        }
    },
    "operations": [
        {
            "id": "list_invoices",
            "upstream": "billing",
            "description": "List invoices for a customer.",
            "binding": {
                "method": "GET",
                "path": "/v1/invoices",
                "parameters": [{"arg": "customer_id", "in": "query", "required": True}],
            },
        }
    ],
}


@pytest.fixture
def upstream_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream_requests: list[httpx.Request],
) -> App:
    monkeypatch.setenv("BILLING_KEY", "sk-test")

    async def handler(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(200, json={"invoices": ["inv_1"]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG))
    return build_app(load_config(path))


async def _session(app: App, body):
    """Run `body(client)` against a real client/server pair over memory streams."""
    server = build_server(app, "billing-portal")
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async with anyio.create_task_group() as tg:

            async def serve() -> None:
                await server.run(
                    server_read,
                    server_write,
                    server.create_initialization_options(),
                    raise_exceptions=True,
                )

            tg.start_soon(serve)
            async with ClientSession(client_read, client_write) as client:
                await client.initialize()
                await body(client)
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_list_tools_round_trips_over_a_real_session(app: App):
    seen: list[types.Tool] = []

    async def body(client: ClientSession) -> None:
        seen.extend((await client.list_tools()).tools)

    try:
        await _session(app, body)
    finally:
        await app.aclose()

    (tool,) = seen
    assert tool.name == "list_invoices"
    assert tool.description == "List invoices for a customer."
    assert tool.input_schema["required"] == ["customer_id"]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True


@pytest.mark.anyio
async def test_call_tool_round_trips_over_a_real_session(
    app: App, upstream_requests: list[httpx.Request]
):
    results: list[types.CallToolResult] = []

    async def body(client: ClientSession) -> None:
        results.append(await client.call_tool("list_invoices", {"customer_id": "cus_1"}))

    try:
        await _session(app, body)
    finally:
        await app.aclose()

    (result,) = results
    assert result.is_error is False
    assert isinstance(result.content[0], types.TextContent)
    assert "inv_1" in result.content[0].text

    # The call really left the process boundary, credential and all.
    (request,) = upstream_requests
    assert request.url.params["customer_id"] == "cus_1"
    assert request.headers["authorization"] == "Bearer sk-test"


@pytest.mark.anyio
async def test_calling_an_unknown_tool_returns_an_error_result_not_a_protocol_error(app: App):
    results: list[types.CallToolResult] = []

    async def body(client: ClientSession) -> None:
        results.append(await client.call_tool("ghost", {}))

    try:
        await _session(app, body)
    finally:
        await app.aclose()

    (result,) = results
    assert result.is_error is True
    assert isinstance(result.content[0], types.TextContent)
    assert "ghost" in result.content[0].text
