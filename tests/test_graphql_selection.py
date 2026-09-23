import pytest

from mcp_portal.sources.graphql_introspection import IntrospectedSchema, parse_introspection_result
from mcp_portal.sources.graphql_selection import SelectionError, build_selection_set

_RESULT = {
    "data": {
        "__schema": {
            "queryType": {"name": "Query"},
            "mutationType": None,
            "types": [
                {"kind": "OBJECT", "name": "Query", "fields": []},
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
                        {
                            "name": "manager",
                            "args": [],
                            "type": {"kind": "OBJECT", "name": "User", "ofType": None},
                        },
                        {
                            "name": "posts",
                            "args": [],
                            "type": {
                                "kind": "LIST",
                                "name": None,
                                "ofType": {"kind": "OBJECT", "name": "Post", "ofType": None},
                            },
                        },
                        {
                            "name": "profile",
                            "args": [
                                {
                                    "name": "size",
                                    "type": {"kind": "SCALAR", "name": "Int", "ofType": None},
                                }
                            ],
                            "type": {"kind": "OBJECT", "name": "Profile", "ofType": None},
                        },
                    ],
                },
                {
                    "kind": "OBJECT",
                    "name": "Post",
                    "fields": [
                        {
                            "name": "title",
                            "args": [],
                            "type": {"kind": "SCALAR", "name": "String", "ofType": None},
                        },
                        {
                            "name": "author",
                            "args": [],
                            "type": {"kind": "OBJECT", "name": "User", "ofType": None},
                        },
                    ],
                },
                {
                    "kind": "OBJECT",
                    "name": "Profile",
                    "fields": [
                        {
                            "name": "bio",
                            "args": [],
                            "type": {"kind": "SCALAR", "name": "String", "ofType": None},
                        },
                    ],
                },
            ],
        }
    }
}


def _schema() -> IntrospectedSchema:
    return parse_introspection_result(_RESULT)


def test_scalar_fields_are_included():
    doc = build_selection_set("User", _schema(), {})
    assert "id" in doc
    assert "ssn" in doc


def test_type_policy_excludes_named_fields():
    doc = build_selection_set("User", _schema(), {"User": ["ssn"]})
    assert "ssn" not in doc
    assert "id" in doc


def test_recursion_stops_on_first_type_reoccurrence():
    doc = build_selection_set("User", _schema(), {})
    # User -> manager -> User: the second User must not re-expand manager/posts.
    assert doc.count("manager") == 1
    # User -> posts -> Post -> author -> User: same rule from the other direction.
    assert doc.count("author") == 1


def test_fields_with_required_or_any_args_are_skipped():
    doc = build_selection_set("User", _schema(), {})
    assert "profile" not in doc


def test_unknown_type_raises():
    with pytest.raises(SelectionError):
        build_selection_set("DoesNotExist", _schema(), {})
