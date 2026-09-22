from mcp_portal.auth.principal import Principal
from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.config.policy import (
    PolicyConfig,
    PolicyDefaults,
    PolicyMatchSpec,
    PolicyOutboundConfig,
    PolicyRule,
)
from mcp_portal.operations import (
    Effect,
    HttpBinding,
    Operation,
    Sensitivity,
)
from mcp_portal.policy import PolicyEngine


def op(
    op_id: str = "initiate_payment",
    effect: Effect = Effect.ACTION,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
    tags: tuple[str, ...] = ("billing",),
    upstream: str = "billing",
) -> Operation:
    return Operation(
        id=op_id,
        upstream=upstream,
        name=op_id,
        title=op_id,
        description="d",
        group_tags=tags,
        effect=effect,
        sensitivity=sensitivity,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method="POST", path="/x"),
    )


def principal(*details: AuthorizationDetail) -> Principal:
    return Principal(identity="local", authorization_details=details)


PAYMENT_RULE: dict = {
    "match": {"tags": ["billing"], "effect": ["action"]},
    "require": {
        "authorization_details": [
            {
                "type": "payment_initiation",
                "actions": ["initiate"],
                "locations": ["https://api.example.com/v1/payments"],
            }
        ]
    },
}


def test_default_policy_allows_everything():
    engine = PolicyEngine(PolicyConfig(version="1"))
    decision = engine.evaluate(op(), principal())
    assert decision.allowed is True
    assert decision.missing == ()


def test_a_non_matching_operation_falls_back_to_unmatched_default_allow():
    engine = PolicyEngine(PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]}))
    decision = engine.evaluate(op(tags=("shipping",)), principal())
    assert decision.allowed is True


def test_a_non_matching_operation_falls_back_to_unmatched_default_deny():
    cfg = PolicyConfig.model_validate(
        {"version": "1", "defaults": {"unmatched": "deny"}, "rules": [PAYMENT_RULE]}
    )
    decision = PolicyEngine(cfg).evaluate(op(tags=("shipping",)), principal())
    assert decision.allowed is False


def test_a_matched_operation_with_covering_details_is_allowed():
    cfg = PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]})
    presented = AuthorizationDetail(
        type="payment_initiation",
        actions=("initiate", "cancel"),
        locations=("https://api.example.com/v1/payments",),
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal(presented))
    assert decision.allowed is True
    assert decision.missing == ()


def test_a_matched_operation_with_no_presented_details_is_denied():
    cfg = PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]})
    decision = PolicyEngine(cfg).evaluate(op(), principal())
    assert decision.allowed is False
    assert decision.missing[0].type == "payment_initiation"


def test_a_matched_operation_with_a_non_covering_detail_is_denied():
    cfg = PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]})
    wrong = AuthorizationDetail(type="payment_initiation", actions=("cancel",))
    decision = PolicyEngine(cfg).evaluate(op(), principal(wrong))
    assert decision.allowed is False


def test_requirements_accumulate_across_every_matching_rule():
    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                PAYMENT_RULE,
                {
                    "match": {"tags": ["billing"]},
                    "require": {
                        "authorization_details": [{"type": "audit_log", "actions": ["write"]}]
                    },
                },
            ],
        }
    )
    presented_payment = AuthorizationDetail(
        type="payment_initiation",
        actions=("initiate",),
        locations=("https://api.example.com/v1/payments",),
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal(presented_payment))
    assert decision.allowed is False
    assert {d.type for d in decision.missing} == {"audit_log"}

    presented_both = (presented_payment, AuthorizationDetail(type="audit_log", actions=("write",)))
    decision = PolicyEngine(cfg).evaluate(op(), principal(*presented_both))
    assert decision.allowed is True


def test_carry_is_true_when_any_matching_rule_sets_it():
    cfg = PolicyConfig.model_validate(
        {"version": "1", "rules": [{"match": {"tags": ["billing"]}, "outbound": {"carry": True}}]}
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal())
    assert decision.allowed is True
    assert decision.carry is True


def test_a_carry_only_rule_does_not_count_as_matched_under_unmatched_deny():
    # PolicyConfig.model_validate would reject this combination (an earlier
    # task's _require_less_rule_forbidden_under_deny validator), so
    # model_construct is used to reach the state directly and verify
    # PolicyEngine's own defense-in-depth: a carry-only rule's match must
    # never count toward `defaults.unmatched`, even though the config loader
    # also independently forbids this shape.
    cfg = PolicyConfig.model_construct(
        version="1",
        defaults=PolicyDefaults(unmatched="deny"),
        rules=[
            PolicyRule(
                match=PolicyMatchSpec(tags=["billing"]),
                outbound=PolicyOutboundConfig(carry=True),
            )
        ],
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal())
    assert decision.allowed is False


def test_carry_is_false_when_no_matching_rule_sets_it():
    cfg = PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]})
    presented = AuthorizationDetail(
        type="payment_initiation",
        actions=("initiate",),
        locations=("https://api.example.com/v1/payments",),
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal(presented))
    assert decision.carry is False


def test_match_on_sensitivity():
    # `defaults.unmatched` is left at its default ("allow") deliberately: this
    # test verifies the `sensitivity` match field in isolation. A NORMAL
    # operation doesn't match the rule at all, so it falls through to the
    # unmatched default (allow) rather than being denied — using
    # `unmatched: deny` here would conflate "did the sensitivity field match"
    # with "what happens to operations no rule mentions," which is a
    # different test (see test_a_non_matching_operation_falls_back_to_unmatched_default_deny).
    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                {
                    "match": {"sensitivity": ["sensitive"]},
                    "require": {"authorization_details": [{"type": "x"}]},
                }
            ],
        }
    )
    engine = PolicyEngine(cfg)
    assert engine.evaluate(op(sensitivity=Sensitivity.NORMAL), principal()).allowed is True
    assert engine.evaluate(op(sensitivity=Sensitivity.SENSITIVE), principal()).allowed is False


def test_match_on_ids_and_upstream():
    # Same reasoning as test_match_on_sensitivity: `defaults.unmatched` stays
    # at "allow" so this test isolates the `ids`/`upstream` match fields
    # rather than re-testing the unmatched-default behavior.
    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                {
                    "match": {"ids": ["export_*"], "upstream": ["billing"]},
                    "require": {"authorization_details": [{"type": "x"}]},
                }
            ],
        }
    )
    engine = PolicyEngine(cfg)
    assert engine.evaluate(op(op_id="export_report"), principal()).allowed is False
    assert engine.evaluate(op(op_id="list_invoices"), principal()).allowed is True


def test_dead_rule_warnings_names_a_rule_matching_nothing():
    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [{"match": {"tags": ["nonexistent"]}, "outbound": {"carry": True}}],
        }
    )
    warnings = PolicyEngine(cfg).dead_rule_warnings([op()])
    assert len(warnings) == 1
    assert "nonexistent" in warnings[0]


def test_dead_rule_warnings_is_empty_when_every_rule_matches_something():
    cfg = PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]})
    assert PolicyEngine(cfg).dead_rule_warnings([op()]) == []
