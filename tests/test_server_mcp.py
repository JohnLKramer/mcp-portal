import dataclasses

import httpx
import pytest
from mcp import types

from mcp_portal.auth.principal import Principal
from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.config.models import UpstreamConfig
from mcp_portal.config.policy import PolicyConfig
from mcp_portal.operations import (
    Effect,
    HttpBinding,
    Operation,
    Parameter,
    ParamLocation,
    Sensitivity,
)
from mcp_portal.policy import PolicyEngine
from mcp_portal.registry import ToolSet
from mcp_portal.server.mcp import ToolInvoker, annotations_for, to_mcp_tool
from mcp_portal.transports.http import HttpTransport


def op(effect: Effect = Effect.READ_ONLY, name: str = "list_invoices") -> Operation:
    return Operation(
        id="list_invoices",
        upstream="billing",
        name=name,
        title="List invoices",
        description="List invoices for a customer.",
        group_tags=("billing",),
        effect=effect,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        binding=HttpBinding(method="GET", path="/v1/invoices"),
    )


def test_operation_converts_to_an_mcp_tool():
    tool = to_mcp_tool(op())
    assert isinstance(tool, types.Tool)
    assert tool.name == "list_invoices"
    assert tool.title == "List invoices"
    assert tool.input_schema["properties"]["q"] == {"type": "string"}


def test_tool_serializes_with_camel_case_wire_names():
    payload = to_mcp_tool(op()).model_dump(by_alias=True, exclude_none=True)
    assert "inputSchema" in payload
    assert payload["annotations"]["readOnlyHint"] is True


@pytest.mark.parametrize(
    ("effect", "read_only", "destructive", "idempotent"),
    [
        (Effect.READ_ONLY, True, False, True),
        (Effect.IDEMPOTENT_WRITE, False, True, True),
        (Effect.ACTION, False, True, False),
    ],
)
def test_annotations_follow_the_effect(effect, read_only, destructive, idempotent):
    ann = annotations_for(op(effect))
    assert ann.read_only_hint is read_only
    assert ann.destructive_hint is destructive
    assert ann.idempotent_hint is idempotent


def test_open_world_hint_is_always_true():
    assert annotations_for(op()).open_world_hint is True


def invoker(handler, operation: Operation) -> ToolInvoker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = UpstreamConfig(base_url="https://api.example.com")
    toolset = ToolSet(operations=(operation,), by_name={operation.name: operation})
    return ToolInvoker(
        toolset=toolset,
        transports={"billing": HttpTransport(client, upstream, None)},
    )


@pytest.mark.anyio
async def test_successful_call_returns_text_content():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"invoices": []})

    result = await invoker(handler, op()).call("list_invoices", {})
    assert result.is_error is False
    assert isinstance(result.content[0], types.TextContent)
    assert "invoices" in result.content[0].text


@pytest.mark.anyio
async def test_non_2xx_sets_is_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    result = await invoker(handler, op()).call("list_invoices", {})
    assert result.is_error is True
    assert "not found" in result.content[0].text


@pytest.mark.anyio
async def test_unknown_tool_is_an_error_result_not_an_exception():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{}")

    result = await invoker(handler, op()).call("ghost", {})
    assert result.is_error is True
    assert "ghost" in result.content[0].text


@pytest.mark.anyio
async def test_invalid_arguments_produce_an_actionable_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{}")

    operation = Operation(
        id="get",
        upstream="billing",
        name="get",
        title="Get",
        description="Get.",
        group_tags=(),
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}, "required": ["needed"]},
        binding=HttpBinding(method="GET", path="/v1/x"),
    )
    result = await invoker(handler, operation).call("get", {})
    assert result.is_error is True
    assert "needed" in result.content[0].text


@pytest.mark.anyio
async def test_build_request_error_produces_an_actionable_error_not_an_exception():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{}")

    operation = Operation(
        id="get_invoice",
        upstream="billing",
        name="get_invoice",
        title="Get invoice",
        description="Get an invoice by id.",
        group_tags=(),
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        binding=HttpBinding(
            method="GET",
            path="/v1/invoices/{id}",
            parameters=(
                Parameter(
                    arg="id",
                    location=ParamLocation.PATH,
                    wire_name="id",
                    required=True,
                    schema={"type": "string"},
                ),
            ),
        ),
    )

    # "" satisfies the {"type": "string"} JSON Schema, but build_request rejects an
    # empty path parameter because it would collapse the route segment.
    result = await invoker(handler, operation).call("get_invoice", {"id": ""})
    assert result.is_error is True
    assert "invalid arguments" in result.content[0].text
    assert "'id'" in result.content[0].text


@pytest.mark.anyio
async def test_upstream_timeout_produces_an_error_result_not_an_exception():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream took too long")

    # Effect.ACTION is not retried, so a single timed-out attempt exhausts the
    # retry budget immediately and HttpTransport.execute re-raises.
    operation = op(effect=Effect.ACTION)
    result = await invoker(handler, operation).call("list_invoices", {})
    assert result.is_error is True
    assert "timed out" in result.content[0].text


@pytest.mark.anyio
async def test_a_non_timeout_transport_failure_produces_an_error_result():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    # ConnectError is an httpx.RequestError but not a TimeoutException, so it only
    # stays inside the tool result if the general supertype is caught.
    result = await invoker(handler, op(effect=Effect.ACTION)).call("list_invoices", {})
    assert result.is_error is True
    assert "request failed" in result.content[0].text
    assert "connection refused" in result.content[0].text


def invoker_with_policy(
    handler, operation: Operation, policy: PolicyEngine, principal: Principal
) -> ToolInvoker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = UpstreamConfig(base_url="https://api.example.com")
    toolset = ToolSet(operations=(operation,), by_name={operation.name: operation})
    return ToolInvoker(
        toolset=toolset,
        transports={"billing": HttpTransport(client, upstream, None)},
        policy=policy,
        principal=principal,
    )


@pytest.mark.anyio
async def test_a_call_with_no_policy_engine_is_unaffected_p1_p2_behavior():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    result = await invoker(handler, op()).call("list_invoices", {})
    assert result.is_error is False


@pytest.mark.anyio
async def test_a_denied_call_never_reaches_the_transport():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                {
                    "match": {"effect": ["action"]},
                    "require": {"authorization_details": [{"type": "payment_initiation"}]},
                }
            ],
        }
    )
    result = await invoker_with_policy(
        handler, op(effect=Effect.ACTION), PolicyEngine(cfg), Principal("local", ())
    ).call("list_invoices", {})

    assert result.is_error is True
    assert "payment_initiation" in result.content[0].text
    assert calls == []


@pytest.mark.anyio
async def test_an_allowed_call_proceeds_to_the_transport():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                {
                    "match": {"effect": ["action"]},
                    "require": {"authorization_details": [{"type": "payment_initiation"}]},
                }
            ],
        }
    )
    principal = Principal("local", (AuthorizationDetail(type="payment_initiation"),))
    result = await invoker_with_policy(
        handler, op(effect=Effect.ACTION), PolicyEngine(cfg), principal
    ).call("list_invoices", {})

    assert result.is_error is False


@pytest.mark.anyio
async def test_invalid_arguments_are_rejected_before_policy_is_even_consulted():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    operation = op()
    schema_op = dataclasses.replace(
        operation,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    )
    cfg = PolicyConfig.model_validate(
        {"version": "1", "defaults": {"unmatched": "deny"}, "rules": []}
    )
    result = await invoker_with_policy(
        handler, schema_op, PolicyEngine(cfg), Principal("local", ())
    ).call("list_invoices", {})

    assert result.is_error is True
    assert "invalid arguments" in result.content[0].text
