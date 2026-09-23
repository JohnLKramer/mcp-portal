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
