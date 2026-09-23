import httpx
import pytest

from mcp_portal.sources.graphql_introspection import (
    GraphQlIntrospectionError,
    fetch_schema,
    parse_introspection_result,
    render_type_ref,
)

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
                    "fields": [],
                },
                {
                    "kind": "OBJECT",
                    "name": "User",
                    "fields": [
                        {
                            "name": "id",
                            "args": [],
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None},
                            },
                        },
                        {
                            "name": "name",
                            "args": [],
                            "type": {"kind": "SCALAR", "name": "String", "ofType": None},
                        },
                    ],
                },
            ],
        }
    }
}


def test_parse_introspection_result_builds_types_by_name():
    schema = parse_introspection_result(_RESULT)
    assert schema.query_type == "Query"
    assert schema.mutation_type == "Mutation"
    user = schema.types_by_name["User"]
    assert [f.name for f in user.fields] == ["id", "name"]


def test_parse_introspection_result_rejects_a_response_with_no_schema():
    with pytest.raises(GraphQlIntrospectionError):
        parse_introspection_result({"data": {}})


def test_render_type_ref_renders_non_null_and_named():
    schema = parse_introspection_result(_RESULT)
    id_arg = schema.types_by_name["Query"].fields[0].args[0]
    assert render_type_ref(id_arg.type) == "ID!"
    name_field = schema.types_by_name["User"].fields[1]
    assert render_type_ref(name_field.type) == "String"


def test_fetch_schema_posts_the_introspection_query_and_parses_the_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        body = request.read()
        assert b"__schema" in body
        return httpx.Response(200, json=_RESULT)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    schema = fetch_schema("https://api.example.com/graphql", client)
    assert schema.query_type == "Query"


def test_fetch_schema_rejects_a_response_carrying_graphql_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "introspection disabled"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GraphQlIntrospectionError, match="introspection disabled"):
        fetch_schema("https://api.example.com/graphql", client)
