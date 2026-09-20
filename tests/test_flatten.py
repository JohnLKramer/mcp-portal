import pytest

from mcp_sidekit.operations import BodyMode, BodySpec, HttpBinding, Parameter, ParamLocation
from mcp_sidekit.sources.flatten import FlattenError, build_input_schema, resolve_arg_names


def param(arg: str, location: ParamLocation, required: bool = True, wire: str | None = None):
    return Parameter(
        arg=arg,
        location=location,
        wire_name=wire or arg,
        required=required,
        schema={"type": "string"},
    )


def test_parameters_flatten_into_one_object_schema():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices/{id}",
        parameters=(
            param("invoice_id", ParamLocation.PATH, wire="id"),
            param("customer_id", ParamLocation.QUERY, required=False),
        ),
    )
    schema = build_input_schema(binding)
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"invoice_id", "customer_id"}
    assert schema["required"] == ["invoice_id"]


def test_body_properties_are_lifted_in_flatten_mode():
    binding = HttpBinding(
        method="POST",
        path="/v1/invoices",
        body=BodySpec(
            content_type="application/json",
            schema={
                "type": "object",
                "properties": {"amount": {"type": "integer"}, "memo": {"type": "string"}},
                "required": ["amount"],
            },
        ),
    )
    schema = build_input_schema(binding)
    assert set(schema["properties"]) == {"amount", "memo"}
    assert schema["required"] == ["amount"]


def test_non_object_body_becomes_a_single_argument():
    binding = HttpBinding(
        method="POST",
        path="/v1/bulk",
        body=BodySpec(content_type="application/json", schema={"type": "array"}),
    )
    schema = build_input_schema(binding)
    assert set(schema["properties"]) == {"body"}
    assert schema["required"] == ["body"]


def test_single_arg_mode_keeps_the_body_whole():
    binding = HttpBinding(
        method="POST",
        path="/v1/invoices",
        body=BodySpec(
            content_type="application/json",
            schema={"type": "object", "properties": {"amount": {"type": "integer"}}},
            mode=BodyMode.SINGLE_ARG,
        ),
    )
    assert set(build_input_schema(binding)["properties"]) == {"body"}


def test_colliding_names_are_prefixed_with_location():
    binding = HttpBinding(
        method="GET",
        path="/v1/things/{id}",
        parameters=(
            param("id", ParamLocation.PATH),
            param("id", ParamLocation.QUERY),
        ),
    )
    resolved = resolve_arg_names(binding)
    assert {p.arg for p in resolved.parameters} == {"path_id", "query_id"}
    # wire_name is untouched: flattening is presentation only
    assert {p.wire_name for p in resolved.parameters} == {"id"}


def test_recollision_after_prefixing_is_an_error_naming_both_contributors():
    binding = HttpBinding(
        method="POST",
        path="/v1/things/{id}",
        parameters=(
            param("id", ParamLocation.PATH),
            param("id", ParamLocation.QUERY),
        ),
        body=BodySpec(
            content_type="application/json",
            schema={"type": "object", "properties": {"query_id": {"type": "string"}}},
        ),
    )
    with pytest.raises(FlattenError) as exc:
        resolve_arg_names(binding)
    assert "query_id" in str(exc.value)
    assert "body" in str(exc.value)


def test_unsupported_content_type_is_an_error():
    binding = HttpBinding(
        method="POST",
        path="/v1/upload",
        body=BodySpec(content_type="multipart/form-data", schema={"type": "object"}),
    )
    with pytest.raises(FlattenError) as exc:
        build_input_schema(binding)
    assert "multipart/form-data" in str(exc.value)


def test_empty_object_body_does_not_spuriously_reserve_body_name():
    """Regression test: empty object body should not treat 'body' as reserved.

    When a FLATTEN-mode body is an object type with no (or empty) properties,
    _body_properties returns {} (falsy but not None). The old code would treat
    'body' as a reserved name due to truthiness check, causing a query param
    named 'body' to be spuriously renamed to 'query_body' even though no
    'body' property exists in the output schema.
    """
    binding = HttpBinding(
        method="POST",
        path="/v1/test",
        parameters=(param("body", ParamLocation.QUERY),),
        body=BodySpec(
            content_type="application/json",
            schema={"type": "object"},  # no properties
        ),
    )
    resolved = resolve_arg_names(binding)
    # Parameter named 'body' should keep its name, not become 'query_body'
    assert {p.arg for p in resolved.parameters} == {"body"}

    schema = build_input_schema(binding)
    # The schema should only have the query parameter, not an extra body argument
    assert set(schema["properties"]) == {"body"}
    assert schema["required"] == ["body"]
