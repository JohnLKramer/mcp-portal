import pytest

from mcp_portal.sources.refs import RefError, RefResolver


def test_internal_ref_resolves_to_its_target():
    doc = {
        "components": {"schemas": {"Amount": {"type": "integer"}}},
        "x": {"$ref": "#/components/schemas/Amount"},
    }
    resolved = RefResolver(doc, allow_external=False).resolve()
    assert resolved["x"] == {"type": "integer"}


def test_ref_nested_inside_a_list_resolves():
    doc = {
        "components": {"schemas": {"Amount": {"type": "integer"}}},
        "items": [{"$ref": "#/components/schemas/Amount"}, {"type": "string"}],
    }
    resolved = RefResolver(doc, allow_external=False).resolve()
    assert resolved["items"] == [{"type": "integer"}, {"type": "string"}]


def test_a_ref_target_that_itself_contains_a_ref_is_resolved_recursively():
    doc = {
        "components": {
            "schemas": {
                "Amount": {"type": "integer"},
                "Money": {
                    "type": "object",
                    "properties": {"amount": {"$ref": "#/components/schemas/Amount"}},
                },
            }
        },
        "x": {"$ref": "#/components/schemas/Money"},
    }
    resolved = RefResolver(doc, allow_external=False).resolve()
    assert resolved["x"]["properties"]["amount"] == {"type": "integer"}


def test_a_broken_pointer_is_a_ref_error():
    doc = {"x": {"$ref": "#/components/schemas/Missing"}}
    with pytest.raises(RefError):
        RefResolver(doc, allow_external=False).resolve()


def test_a_direct_self_reference_is_replaced_with_an_open_object_and_warns():
    doc = {"components": {"schemas": {"Node": {"type": "object", "properties": {"child": {}}}}}}
    doc["components"]["schemas"]["Node"]["properties"]["child"] = {
        "$ref": "#/components/schemas/Node"
    }
    resolver = RefResolver(doc, allow_external=False)
    resolved = resolver.resolve()
    node = resolved["components"]["schemas"]["Node"]
    assert node["properties"]["child"] == {"type": "object"}
    assert any("circular" in w for w in resolver.warnings)


def test_a_networked_external_ref_is_rejected_by_default():
    doc = {"x": {"$ref": "https://evil.example.com/frag.yaml#/Foo"}}
    with pytest.raises(RefError) as exc:
        RefResolver(doc, allow_external=False).resolve()
    assert "external" in str(exc.value)


def test_a_networked_external_ref_outside_the_host_allowlist_is_rejected():
    doc = {"x": {"$ref": "https://evil.example.com/frag.yaml#/Foo"}}
    with pytest.raises(RefError):
        RefResolver(
            doc, allow_external=True, allowed_hosts=frozenset({"trusted.example.com"})
        ).resolve()


def test_a_networked_external_ref_within_the_allowlist_is_fetched():
    fetched = {"Foo": {"type": "integer"}}
    doc = {"x": {"$ref": "https://trusted.example.com/frag.yaml#/Foo"}}
    resolver = RefResolver(
        doc,
        allow_external=True,
        allowed_hosts=frozenset({"trusted.example.com"}),
        fetch_external=lambda target: fetched,
    )
    assert resolver.resolve()["x"] == {"type": "integer"}


def test_a_relative_file_external_ref_is_rejected_by_default():
    doc = {"x": {"$ref": "common.yaml#/Foo"}}
    with pytest.raises(RefError):
        RefResolver(doc, allow_external=False).resolve()


def test_a_relative_file_external_ref_is_permitted_with_a_fetcher_when_allowed():
    fetched = {"Foo": {"type": "string"}}
    doc = {"x": {"$ref": "common.yaml#/Foo"}}
    resolver = RefResolver(doc, allow_external=True, fetch_external=lambda target: fetched)
    assert resolver.resolve()["x"] == {"type": "string"}


def test_external_documents_are_fetched_once_and_cached():
    calls: list[str] = []

    def fetch(target: str) -> dict:
        calls.append(target)
        return {"Foo": {"type": "string"}, "Bar": {"type": "integer"}}

    doc = {
        "x": {"$ref": "common.yaml#/Foo"},
        "y": {"$ref": "common.yaml#/Bar"},
    }
    RefResolver(doc, allow_external=True, fetch_external=fetch).resolve()
    assert calls == ["common.yaml"]
