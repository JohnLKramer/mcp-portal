import pytest

from mcp_portal.naming import (
    MAX_NAME_LENGTH,
    NAME_PATTERN,
    NameCollisionError,
    NamingOptions,
    generate_names,
    normalize,
)
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity


def op(op_id: str, upstream: str = "billing", tags: tuple[str, ...] = ("billing",)) -> Operation:
    return Operation(
        id=op_id,
        upstream=upstream,
        name="",
        title=op_id,
        description="",
        group_tags=tags,
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method="GET", path="/x"),
    )


def test_normalize_lowercases_and_replaces_separators():
    assert normalize("List-Invoices By.Customer") == "list_invoices_by_customer"


def test_operation_id_strategy_uses_the_id():
    names = generate_names([op("listInvoices")], NamingOptions())
    assert names["listInvoices"] == "listinvoices"


def test_group_tag_prefix_uses_the_first_tag_in_document_order():
    options = NamingOptions(prefix_with_group_tag=True)
    names = generate_names([op("list", tags=("billing", "admin"))], options)
    assert names["list"] == "billing_list"


def test_operation_with_no_tags_gets_no_prefix():
    options = NamingOptions(prefix_with_group_tag=True)
    names = generate_names([op("list", tags=())], options)
    assert names["list"] == "list"


def test_upstream_prefix_precedes_group_tag_prefix():
    options = NamingOptions(prefix_with_group_tag=True, prefix_with_upstream=True)
    names = generate_names([op("list")], options)
    assert names["list"] == "billing_billing_list"


def test_collisions_suffix_every_collider_deterministically():
    ops = [op("list-invoices"), op("list_invoices")]
    names = generate_names(ops, NamingOptions())
    assert names["list-invoices"] != names["list_invoices"]
    assert all(n.startswith("list_invoices_") for n in names.values())
    assert names == generate_names(ops, NamingOptions())


def test_long_names_are_truncated_and_suffixed_within_the_cap():
    names = generate_names([op("a" * 200)], NamingOptions())
    assert len(names["a" * 200]) <= MAX_NAME_LENGTH


def test_names_always_match_the_mcp_name_pattern():
    names = generate_names([op("Weird Name!! 99"), op("x" * 100)], NamingOptions())
    for name in names.values():
        assert NAME_PATTERN.fullmatch(name), name


def test_name_pattern_is_anchored():
    assert NAME_PATTERN.search("Bad Name!") is None


def test_an_id_with_no_nameable_characters_still_gets_a_name():
    names = generate_names([op("!!!")], NamingOptions())
    assert NAME_PATTERN.fullmatch(names["!!!"]), names["!!!"]


def test_ids_that_both_normalize_away_get_distinct_names():
    names = generate_names([op("!!!"), op("???")], NamingOptions())
    assert names["!!!"] != names["???"]


def test_unresolvable_collision_raises_rather_than_renaming_again():
    # Two ids whose normalized form AND hash suffix would have to coincide is
    # impossible in practice, so we force it: identical ids are a config error.
    with pytest.raises(NameCollisionError):
        generate_names([op("dup"), op("dup")], NamingOptions())
