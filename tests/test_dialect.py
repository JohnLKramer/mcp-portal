from mcp_portal.sources.dialect import convert_30_schema, is_openapi_31


def test_openapi_31_is_recognized_by_prefix():
    assert is_openapi_31("3.1.0") is True
    assert is_openapi_31("3.1.1") is True
    assert is_openapi_31("3.0.3") is False


def test_nullable_true_widens_type_to_include_null():
    schema = convert_30_schema({"type": "string", "nullable": True})
    assert schema["type"] == ["null", "string"]
    assert "nullable" not in schema


def test_nullable_false_is_just_dropped():
    schema = convert_30_schema({"type": "string", "nullable": False})
    assert schema == {"type": "string"}


def test_boolean_exclusive_minimum_true_becomes_the_numeric_form():
    schema = convert_30_schema({"type": "integer", "minimum": 0, "exclusiveMinimum": True})
    assert schema == {"type": "integer", "exclusiveMinimum": 0}


def test_boolean_exclusive_minimum_false_keeps_minimum_inclusive():
    schema = convert_30_schema({"type": "integer", "minimum": 0, "exclusiveMinimum": False})
    assert schema == {"type": "integer", "minimum": 0}


def test_boolean_exclusive_maximum_true_becomes_the_numeric_form():
    schema = convert_30_schema({"type": "integer", "maximum": 10, "exclusiveMaximum": True})
    assert schema == {"type": "integer", "exclusiveMaximum": 10}


def test_singular_example_becomes_an_examples_array():
    schema = convert_30_schema({"type": "string", "example": "abc"})
    assert schema == {"type": "string", "examples": ["abc"]}


def test_conversion_recurses_into_nested_properties():
    schema = convert_30_schema(
        {
            "type": "object",
            "properties": {"amount": {"type": "integer", "nullable": True}},
        }
    )
    assert schema["properties"]["amount"]["type"] == ["integer", "null"]


def test_conversion_recurses_into_array_items():
    schema = convert_30_schema({"type": "array", "items": {"type": "string", "nullable": True}})
    assert schema["items"]["type"] == ["null", "string"]


def test_non_dict_non_list_values_pass_through_unchanged():
    assert convert_30_schema("not a schema") == "not a schema"
    assert convert_30_schema(42) == 42


def test_a_schema_with_none_of_the_30_isms_is_unchanged():
    schema = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
    assert convert_30_schema(schema) == schema
