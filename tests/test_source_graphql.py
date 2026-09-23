from mcp_portal.operations import Effect, GraphQlBinding
from mcp_portal.sources.graphql import GraphQlSource
from mcp_portal.sources.graphql_introspection import parse_introspection_result

_RESULT = {
    "data": {
        "__schema": {
            "queryType": {"name": "Query"},
            "mutationType": {"name": "Mutation"},
            "types": [
                {
                    "kind": "OBJECT",
                    "name": "Query",
                    "fields": [
                        {
                            "name": "user",
                            "args": [
                                {
                                    "name": "id",
                                    "type": {
                                        "kind": "NON_NULL",
                                        "name": None,
                                        "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None},
                                    },
                                }
                            ],
                            "type": {"kind": "OBJECT", "name": "User", "ofType": None},
                        }
                    ],
                },
                {
                    "kind": "OBJECT",
                    "name": "Mutation",
                    "fields": [
                        {
                            "name": "createUser",
                            "args": [
                                {
                                    "name": "name",
                                    "type": {"kind": "SCALAR", "name": "String", "ofType": None},
                                }
                            ],
                            "type": {"kind": "OBJECT", "name": "User", "ofType": None},
                        },
                        {
                            "name": "deleteUser",
                            "args": [
                                {
                                    "name": "id",
                                    "type": {
                                        "kind": "NON_NULL",
                                        "name": None,
                                        "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None},
                                    },
                                }
                            ],
                            "type": {"kind": "SCALAR", "name": "Boolean", "ofType": None},
                        },
                        {
                            "name": "orphanCreate",
                            "args": [],
                            "type": {"kind": "OBJECT", "name": "Nonexistent", "ofType": None},
                        },
                    ],
                },
                {
                    "kind": "OBJECT",
                    "name": "User",
                    "fields": [
                        {
                            "name": "id",
                            "args": [],
                            "type": {"kind": "SCALAR", "name": "ID", "ofType": None},
                        },
                        {
                            "name": "ssn",
                            "args": [],
                            "type": {"kind": "SCALAR", "name": "String", "ofType": None},
                        },
                    ],
                },
            ],
        }
    }
}


def test_one_operation_per_top_level_query_and_mutation_field():
    schema = parse_introspection_result(_RESULT)
    source = GraphQlSource("my-graphql-api", schema, type_policy={})
    ops = list(source.operations())
    assert {op.id for op in ops} == {"user", "createUser", "deleteUser"}


def test_query_classifies_read_only_and_mutation_classifies_action():
    schema = parse_introspection_result(_RESULT)
    source = GraphQlSource("my-graphql-api", schema, type_policy={})
    by_id = {op.id: op for op in source.operations()}
    assert by_id["user"].effect is Effect.READ_ONLY
    assert by_id["createUser"].effect is Effect.ACTION


def test_variables_become_input_schema_properties():
    schema = parse_introspection_result(_RESULT)
    source = GraphQlSource("my-graphql-api", schema, type_policy={})
    by_id = {op.id: op for op in source.operations()}
    assert "id" in by_id["user"].input_schema["properties"]
    assert by_id["user"].input_schema["required"] == ["id"]


def test_type_policy_excludes_field_from_the_generated_document():
    schema = parse_introspection_result(_RESULT)
    source = GraphQlSource("my-graphql-api", schema, type_policy={"User": ["ssn"]})
    by_id = {op.id: op for op in source.operations()}
    binding = by_id["user"].binding
    assert isinstance(binding, GraphQlBinding)
    assert "ssn" not in binding.document


def test_field_with_unknown_return_type_is_skipped_not_fatal():
    # "orphanCreate" returns "Nonexistent", which has no entry in
    # types_by_name — build_selection_set raises SelectionError for it. That
    # must not abort the whole generator: every other field must still be
    # produced.
    schema = parse_introspection_result(_RESULT)
    source = GraphQlSource("my-graphql-api", schema, type_policy={})
    ops = list(source.operations())
    ids = {op.id for op in ops}
    assert "orphanCreate" not in ids
    assert {"user", "createUser", "deleteUser"} <= ids


def test_scalar_returning_field_has_no_selection_set():
    schema = parse_introspection_result(_RESULT)
    source = GraphQlSource("my-graphql-api", schema, type_policy={})
    by_id = {op.id: op for op in source.operations()}
    binding = by_id["deleteUser"].binding
    assert isinstance(binding, GraphQlBinding)
    assert binding.document == "mutation($id: ID!) { deleteUser(id: $id) }"


def test_int_typed_argument_gets_an_integer_json_schema_type():
    # createUser("name": String) has no Int arg in _RESULT; use a dedicated
    # schema so the mapping is exercised end to end, not just at the
    # unit-helper level.
    schema = parse_introspection_result(
        {
            "data": {
                "__schema": {
                    "queryType": {"name": "Query"},
                    "mutationType": None,
                    "types": [
                        {
                            "kind": "OBJECT",
                            "name": "Query",
                            "fields": [
                                {
                                    "name": "posts",
                                    "args": [
                                        {
                                            "name": "limit",
                                            "type": {
                                                "kind": "NON_NULL",
                                                "name": None,
                                                "ofType": {
                                                    "kind": "SCALAR",
                                                    "name": "Int",
                                                    "ofType": None,
                                                },
                                            },
                                        }
                                    ],
                                    "type": {"kind": "SCALAR", "name": "String", "ofType": None},
                                }
                            ],
                        }
                    ],
                }
            }
        }
    )
    source = GraphQlSource("my-graphql-api", schema, type_policy={})
    by_id = {op.id: op for op in source.operations()}
    assert by_id["posts"].input_schema["properties"]["limit"] == {"type": "integer"}
    assert by_id["posts"].input_schema["required"] == ["limit"]


def test_optional_argument_is_not_marked_required():
    # createUser("name": String) is nullable — no NON_NULL wrapper — so it
    # must be present in `properties` but absent from `required`.
    schema = parse_introspection_result(_RESULT)
    source = GraphQlSource("my-graphql-api", schema, type_policy={})
    by_id = {op.id: op for op in source.operations()}
    input_schema = by_id["createUser"].input_schema
    assert "name" in input_schema["properties"]
    assert "name" not in input_schema["required"]


def test_field_description_is_used_when_present():
    schema = parse_introspection_result(
        {
            "data": {
                "__schema": {
                    "queryType": {"name": "Query"},
                    "mutationType": None,
                    "types": [
                        {
                            "kind": "OBJECT",
                            "name": "Query",
                            "fields": [
                                {
                                    "name": "ping",
                                    "description": "Health-check the upstream.",
                                    "args": [],
                                    "type": {"kind": "SCALAR", "name": "String", "ofType": None},
                                }
                            ],
                        }
                    ],
                }
            }
        }
    )
    source = GraphQlSource("my-graphql-api", schema, type_policy={})
    by_id = {op.id: op for op in source.operations()}
    assert by_id["ping"].description == "Health-check the upstream."
    assert by_id["ping"].title == "Health-check the upstream."


def test_field_without_description_falls_back_to_field_name():
    schema = parse_introspection_result(_RESULT)
    source = GraphQlSource("my-graphql-api", schema, type_policy={})
    by_id = {op.id: op for op in source.operations()}
    assert by_id["user"].description == "user"
    assert by_id["user"].title == "user"
