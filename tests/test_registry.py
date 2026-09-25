import dataclasses

import pytest

from mcp_portal.config.models import McpPortalConfig
from mcp_portal.naming import NameCollisionError
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity
from mcp_portal.registry import build_toolset

BASE: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "s", "transport": "stdio"},
    "upstreams": {"billing": {"base_url": "https://api.example.com"}},
}


def op(op_id: str, tags: tuple[str, ...] = ("billing",), effect: Effect = Effect.READ_ONLY):
    return Operation(
        id=op_id,
        upstream="billing",
        name="",
        title=op_id,
        description="d",
        group_tags=tags,
        effect=effect,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method="GET", path="/x"),
    )


def cfg(**overrides) -> McpPortalConfig:
    return McpPortalConfig.model_validate(BASE | overrides)


def test_names_are_assigned_from_the_surviving_set():
    ts = build_toolset([op("list_invoices")], cfg())
    assert ts.operations[0].name == "list_invoices"
    assert ts.by_name["list_invoices"].id == "list_invoices"


def test_include_tags_is_any_match():
    ops = [op("a", tags=("billing", "x")), op("b", tags=("other",))]
    ts = build_toolset(ops, cfg(selection={"include_tags": ["billing"]}))
    assert [o.id for o in ts.operations] == ["a"]


def test_exclusions_win_over_inclusions():
    ops = [op("a", tags=("billing",))]
    ts = build_toolset(
        ops, cfg(selection={"include_tags": ["billing"], "exclude_tags": ["billing"]})
    )
    assert ts.operations == ()


def test_exclude_ids_supports_globs():
    ops = [op("list_public"), op("list_internal")]
    ts = build_toolset(ops, cfg(selection={"exclude_ids": ["*_internal"]}))
    assert [o.id for o in ts.operations] == ["list_public"]


def test_classification_rule_sets_sensitivity_by_id_glob():
    ops = [op("get_user_ssn"), op("list_invoices")]
    ts = build_toolset(
        ops, cfg(classification=[{"match": {"ids": ["*_ssn"]}, "sensitivity": "sensitive"}])
    )
    by_id = {o.id: o for o in ts.operations}
    assert by_id["get_user_ssn"].sensitivity is Sensitivity.SENSITIVE
    assert by_id["list_invoices"].sensitivity is Sensitivity.NORMAL


def test_later_classification_rules_win():
    ops = [op("x", tags=("admin",))]
    ts = build_toolset(
        ops,
        cfg(
            classification=[
                {"match": {"tags": ["admin"]}, "sensitivity": "sensitive"},
                {"match": {"ids": ["x"]}, "sensitivity": "normal"},
            ]
        ),
    )
    assert ts.operations[0].sensitivity is Sensitivity.NORMAL


def test_classification_can_override_effect():
    ops = [op("reindex", effect=Effect.IDEMPOTENT_WRITE)]
    ts = build_toolset(
        ops, cfg(classification=[{"match": {"ids": ["reindex"]}, "effect": "action"}])
    )
    assert ts.operations[0].effect is Effect.ACTION


def test_classification_runs_before_selection():
    # A rule marks the op sensitive; selection excludes it by tag afterwards.
    ops = [op("a", tags=("billing",))]
    ts = build_toolset(
        ops,
        cfg(
            classification=[{"match": {"ids": ["a"]}, "sensitivity": "sensitive"}],
            selection={"exclude_tags": ["billing"]},
        ),
    )
    assert ts.operations == ()


def test_a_selection_rule_matching_nothing_warns_but_does_not_fail():
    ts = build_toolset([op("a")], cfg(selection={"exclude_ids": ["ghost_*"]}))
    assert any("ghost_*" in w for w in ts.warnings)
    assert [o.id for o in ts.operations] == ["a"]


def test_naming_options_flow_from_config():
    ts = build_toolset([op("list")], cfg(naming={"prefix_with_group_tag": True}))
    assert ts.operations[0].name == "billing_list"


def named(op_id: str, name: str) -> Operation:
    return dataclasses.replace(op(op_id), name=name)


def test_an_explicit_name_is_used_verbatim():
    ts = build_toolset([named("list_invoices", "invoices")], cfg())
    assert ts.operations[0].name == "invoices"
    assert ts.by_name["invoices"].id == "list_invoices"


def test_two_operations_sharing_an_explicit_name_collide():
    ops = [named("a", "invoices"), named("b", "invoices")]
    with pytest.raises(NameCollisionError) as exc:
        build_toolset(ops, cfg())
    assert "invoices" in str(exc.value)


def test_an_explicit_name_colliding_with_a_generated_one_is_an_error():
    # "b" would generate the name "b"; "a" claims it explicitly first.
    ops = [named("a", "b"), op("b")]
    with pytest.raises(NameCollisionError) as exc:
        build_toolset(ops, cfg())
    assert "'b'" in str(exc.value)


def test_duplicate_operation_ids_collide_even_when_one_is_explicitly_named():
    ops = [named("dup", "one"), op("dup")]
    with pytest.raises(NameCollisionError) as exc:
        build_toolset(ops, cfg())
    assert "dup" in str(exc.value)


def test_an_explicit_name_does_not_perturb_other_generated_names():
    # "a" takes an explicit name, so "list_invoices" keeps its clean generated one
    # instead of being suffixed against a name nothing publishes.
    ts = build_toolset([named("a", "z"), op("list_invoices")], cfg())
    assert {o.id: o.name for o in ts.operations} == {"a": "z", "list_invoices": "list_invoices"}


def test_introspect_safe_keeps_only_read_only_normal_sensitivity_operations():
    ops = [
        op("a", effect=Effect.READ_ONLY),
        op("b", effect=Effect.ACTION),
    ]
    ts = build_toolset(ops, cfg(mode="introspect-safe"))
    assert [o.id for o in ts.operations] == ["a"]


def test_introspect_safe_excludes_sensitive_read_only_operations():
    sensitive = dataclasses.replace(
        op("a", effect=Effect.READ_ONLY), sensitivity=Sensitivity.SENSITIVE
    )
    ts = build_toolset([sensitive, op("b")], cfg(mode="introspect-safe"))
    assert [o.id for o in ts.operations] == ["b"]


def test_configured_mode_applies_no_posture_filter():
    ops = [op("a", effect=Effect.ACTION)]
    ts = build_toolset(ops, cfg())
    assert [o.id for o in ts.operations] == ["a"]


def test_introspect_unsafe_applies_no_posture_filter():
    ops = [op("a", effect=Effect.ACTION)]
    ts = build_toolset(ops, cfg(mode="introspect-unsafe", acknowledge_unsafe=True))
    assert [o.id for o in ts.operations] == ["a"]


def test_posture_runs_before_selection():
    # introspect-safe drops "b" for being an action; selection would otherwise
    # have kept it. If posture ran after selection, "b" would survive.
    ops = [op("a", effect=Effect.READ_ONLY), op("b", effect=Effect.ACTION)]
    ts = build_toolset(ops, cfg(mode="introspect-safe", selection={"include_ids": ["a", "b"]}))
    assert [o.id for o in ts.operations] == ["a"]
