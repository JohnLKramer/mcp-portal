import httpx
import pytest
from mcp import types

from mcp_portal.config.models import UpstreamConfig
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity
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
