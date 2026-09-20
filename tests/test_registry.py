from mcp_portal.config.models import Config
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


def cfg(**overrides) -> Config:
    return Config.model_validate(BASE | overrides)


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
