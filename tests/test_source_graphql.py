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
                        }
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
    assert {op.id for op in ops} == {"user", "createUser"}


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
