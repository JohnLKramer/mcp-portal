import dataclasses

import pytest

from mcp_portal.operations import (
    BodyMode,
    BodySpec,
    Effect,
    HttpBinding,
    Operation,
    Parameter,
    ParamLocation,
    Sensitivity,
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
