import dataclasses

import pytest

from mcp_portal.operations import (
    BodyMode,
    BodySpec,
    Effect,
    GraphQlBinding,
    HttpBinding,
    Operation,
    Parameter,
    ParamLocation,
    Sensitivity,
    Variable,
)


def make_binding(**overrides: object) -> HttpBinding:
    defaults: dict[str, object] = {
        "method": "GET",
        "path": "/v1/invoices/{id}",
        "parameters": (
            Parameter(
                arg="invoice_id",
                location=ParamLocation.PATH,
                wire_name="id",
                required=True,
                schema={"type": "string"},
            ),
        ),
        "body": None,
    }
    return HttpBinding(**(defaults | overrides))  # type: ignore[arg-type]


def test_effect_and_sensitivity_are_string_enums():
    assert Effect.READ_ONLY == "read_only"
    assert Effect.IDEMPOTENT_WRITE == "idempotent_write"
    assert Effect.ACTION == "action"
    assert Sensitivity.NORMAL == "normal"
    assert Sensitivity.SENSITIVE == "sensitive"


def test_binding_carries_protocol_discriminator():
    assert make_binding().protocol == "http"


def test_operation_is_frozen():
    op = Operation(
        id="list_invoices",
        upstream="billing",
        name="billing_list_invoices",
        title="List invoices",
        description="List invoices for a customer.",
        group_tags=("billing",),
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=make_binding(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        op.name = "other"  # type: ignore[misc]


def test_body_spec_defaults_to_flatten_mode():
    body = BodySpec(content_type="application/json", schema={"type": "object"})
    assert body.mode is BodyMode.FLATTEN


def test_parameter_defaults_match_openapi_query_defaults():
    param = Parameter(
        arg="customer_id",
        location=ParamLocation.QUERY,
        wire_name="customerId",
        required=True,
        schema={"type": "string"},
    )
    assert param.style == "form"
    assert param.explode is True


def make_graphql_binding(**overrides: object) -> GraphQlBinding:
    defaults: dict[str, object] = {
        "operation_type": "query",
        "document": "query GetUser($id: ID!) { user(id: $id) { id name } }",
        "variables": (Variable(name="id", graphql_type="ID!", required=True),),
    }
    return GraphQlBinding(**(defaults | overrides))  # type: ignore[arg-type]


def test_graphql_binding_is_frozen_and_defaults_to_no_variables():
    binding = GraphQlBinding(operation_type="mutation", document="mutation { noop }")
    assert binding.protocol == "graphql"
    assert binding.variables == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.document = "mutation { other }"  # type: ignore[misc]


def test_operation_accepts_a_graphql_binding():
    op = dataclasses.replace(
        Operation(
            id="get-user",
            upstream="my-graphql-api",
            name="get_user",
            title="Get user",
            description="Fetch a user by id",
            group_tags=(),
            effect=Effect.READ_ONLY,
            sensitivity=Sensitivity.NORMAL,
            input_schema={"type": "object", "properties": {}},
            binding=make_binding(),
        ),
        binding=make_graphql_binding(),
    )
    assert isinstance(op.binding, GraphQlBinding)
