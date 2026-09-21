from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity
from mcp_portal.sources.merge import merge_operations


def op(op_id: str, title: str = "t") -> Operation:
    return Operation(
        id=op_id,
        upstream="billing",
        name="",
        title=title,
        description="d",
        group_tags=(),
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method="GET", path="/x"),
    )


def test_an_explicit_entry_with_a_new_id_is_appended():
    merged = merge_operations([op("a")], [op("b")])
    assert [o.id for o in merged] == ["a", "b"]


def test_an_explicit_entry_with_a_matching_id_replaces_the_introspected_one():
    merged = merge_operations([op("a", title="from-openapi")], [op("a", title="from-config")])
    assert [o.title for o in merged] == ["from-config"]


def test_introspected_order_is_preserved_and_new_explicit_entries_come_last():
    merged = merge_operations([op("a"), op("b")], [op("c"), op("a", title="patched")])
    assert [o.id for o in merged] == ["a", "b", "c"]
    assert merged[0].title == "patched"


def test_no_introspected_operations_is_just_the_explicit_list():
    merged = merge_operations([], [op("a"), op("b")])
    assert [o.id for o in merged] == ["a", "b"]


def test_no_explicit_operations_is_just_the_introspected_list():
    merged = merge_operations([op("a"), op("b")], [])
    assert [o.id for o in merged] == ["a", "b"]
