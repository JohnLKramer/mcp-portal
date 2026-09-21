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
    # The cycle is only detectable once something *points at* Node — chain
    # tracking follows $ref expansion, not raw document position, so an
    # entry-point $ref is what puts "Node" on the chain before its own
    # self-reference is reached.
    doc = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Node"}},
                }
            }
        },
        "x": {"$ref": "#/components/schemas/Node"},
    }
    resolver = RefResolver(doc, allow_external=False)
    resolved = resolver.resolve()
    assert resolved["x"]["properties"]["child"] == {"type": "object"}
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


def test_a_bare_ref_inside_an_external_document_resolves_against_that_document():
    fetched = {"Foo": {"$ref": "#/Bar"}, "Bar": {"type": "integer"}}
    doc = {"x": {"$ref": "common.yaml#/Foo"}}
    resolver = RefResolver(doc, allow_external=True, fetch_external=lambda target: fetched)
    assert resolver.resolve()["x"] == {"type": "integer"}


def test_a_pointer_traversing_through_a_scalar_is_a_ref_error_not_a_crash():
    doc = {"x": {"$ref": "#/foo/bar"}, "foo": "not a container"}
    with pytest.raises(RefError):
        RefResolver(doc, allow_external=False).resolve()


def test_identical_ref_text_across_two_documents_is_not_a_false_cycle():
    top = {
        "components": {"schemas": {"Foo": {"$ref": "other.yaml#/Baz"}}},
        "y": {"$ref": "#/components/schemas/Foo"},
    }
    external = {
        "Baz": {"$ref": "#/components/schemas/Foo"},
        "components": {"schemas": {"Foo": {"type": "string", "external": True}}},
    }
    resolver = RefResolver(top, allow_external=True, fetch_external=lambda target: external)
    assert resolver.resolve()["y"] == {"type": "string", "external": True}
    assert resolver.warnings == []
