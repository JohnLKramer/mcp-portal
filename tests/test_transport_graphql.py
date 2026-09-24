import asyncio
import json

import httpx
import pytest

from mcp_portal.config.models import UpstreamConfig
from mcp_portal.operations import Effect, GraphQlBinding, Operation, Sensitivity, Variable
from mcp_portal.transports.graphql import GraphQlTransport, build_graphql_request
from mcp_portal.transports.http import Credential, StaticCredentialSource


def make_op(**overrides: object) -> Operation:
    binding = GraphQlBinding(
        operation_type="query",
        document="query GetUser($id: ID!) { user(id: $id) { id name } }",
        variables=(Variable(name="id", graphql_type="ID!", required=True),),
    )
    defaults: dict[str, object] = dict(
        id="get_user",
        upstream="gql",
        name="get_user",
        title="Get user",
        description="d",
        group_tags=(),
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        binding=binding,
    )
    return Operation(**(defaults | overrides))  # type: ignore[arg-type]


def test_build_graphql_request_posts_query_and_variables():
    request = build_graphql_request(
        make_op().binding, "https://api.example.com/graphql", {"id": "u1"}, None
    )
    assert request.method == "POST"
    assert request.url == "https://api.example.com/graphql"
    body = json.loads(request.body)
    assert body == {"query": make_op().binding.document, "variables": {"id": "u1"}}
    assert request.headers["Content-Type"] == "application/json"


def test_build_graphql_request_attaches_credential_last():
    credential = Credential(header="Authorization", value="Bearer abc")
    request = build_graphql_request(
        make_op().binding, "https://api.example.com/graphql", {"id": "u1"}, credential
    )
    assert request.headers["Authorization"] == "Bearer abc"


@pytest.mark.anyio
async def test_execute_maps_a_successful_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"user": {"id": "u1", "name": "Ada"}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = UpstreamConfig(base_url="https://api.example.com")
    transport = GraphQlTransport(client, upstream, StaticCredentialSource(None))
    response = await transport.execute(make_op(), {"id": "u1"})
    assert response.status == 200
    assert "Ada" in response.text


@pytest.mark.anyio
async def test_execute_treats_a_graphql_errors_array_as_failure_even_under_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": None, "errors": [{"message": "not found"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = UpstreamConfig(base_url="https://api.example.com")
    transport = GraphQlTransport(client, upstream, StaticCredentialSource(None))
    response = await transport.execute(make_op(), {"id": "u1"})
    assert not (200 <= response.status < 300)
    assert "not found" in response.text


@pytest.mark.anyio
async def test_mutation_is_never_retried_on_a_retryable_status():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = UpstreamConfig(base_url="https://api.example.com")
    transport = GraphQlTransport(client, upstream, StaticCredentialSource(None))
    op = make_op(effect=Effect.ACTION)
    await transport.execute(op, {"id": "u1"})
    assert attempts == 1


@pytest.mark.anyio
async def test_execute_treats_a_truncated_errors_response_as_failure():
    # A large `errors` body gets truncated by `map_body` before display, but
    # the failure classification must not depend on that: it has to be
    # decided from the raw body, not the (possibly truncated) displayed text.
    huge_message = "x" * 2000
    payload = json.dumps({"data": None, "errors": [{"message": huge_message}]}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "application/json"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = UpstreamConfig(base_url="https://api.example.com", max_response_bytes=100)
    transport = GraphQlTransport(client, upstream, StaticCredentialSource(None))
    response = await transport.execute(make_op(), {"id": "u1"})
    assert response.truncated
    assert not (200 <= response.status < 300)


@pytest.mark.anyio
async def test_the_total_time_budget_stops_retrying_before_the_attempt_budget():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(503, text="down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # The per-attempt timeout leaves room for all three attempts; the total
    # budget is spent by the end of the first one.
    upstream = UpstreamConfig(base_url="https://api.example.com", timeout_ms=5000, max_total_ms=10)
    transport = GraphQlTransport(client, upstream, StaticCredentialSource(None))
    result = await transport.execute(make_op(), {"id": "u1"})
    assert calls == 1
    assert result.status == 503


@pytest.mark.anyio
async def test_a_generous_total_budget_leaves_the_attempt_budget_intact():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="bad gateway")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = UpstreamConfig(
        base_url="https://api.example.com", timeout_ms=5000, max_total_ms=60000
    )
    transport = GraphQlTransport(client, upstream, StaticCredentialSource(None))
    await transport.execute(make_op(), {"id": "u1"})
    assert calls == 3
