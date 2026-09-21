import pytest
from pydantic import ValidationError

from mcp_portal.config.policy import PolicyConfig

MINIMAL: dict = {"version": "1"}


def test_minimal_policy_defaults_to_allow_and_no_rules():
    cfg = PolicyConfig.model_validate(MINIMAL)
    assert cfg.defaults.unmatched == "allow"
    assert cfg.rules == []


def test_unmatched_may_be_set_to_deny():
    cfg = PolicyConfig.model_validate(MINIMAL | {"defaults": {"unmatched": "deny"}})
    assert cfg.defaults.unmatched == "deny"


def test_unmatched_rejects_anything_outside_allow_or_deny():
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(MINIMAL | {"defaults": {"unmatched": "warn"}})


def _rule(**overrides) -> dict:
    rule = {
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
    return rule | overrides


def test_a_well_formed_rule_validates():
    cfg = PolicyConfig.model_validate(MINIMAL | {"rules": [_rule()]})
    rule = cfg.rules[0]
    assert rule.match.tags == ["billing"]
    assert rule.require.authorization_details[0].type == "payment_initiation"
    assert rule.outbound.carry is False


def test_outbound_carry_defaults_to_false_and_can_be_set():
    cfg = PolicyConfig.model_validate(MINIMAL | {"rules": [_rule(outbound={"carry": True})]})
    assert cfg.rules[0].outbound.carry is True


def test_match_accepts_ids_tags_upstream_effect_and_sensitivity():
    rule = _rule(
        match={
            "ids": ["get_user_ssn", "export_*"],
            "tags": ["admin"],
            "upstream": ["billing"],
            "effect": ["action"],
            "sensitivity": ["sensitive"],
        }
    )
    cfg = PolicyConfig.model_validate(MINIMAL | {"rules": [rule]})
    assert cfg.rules[0].match.sensitivity == ["sensitive"]


def test_match_has_no_name_field():
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(MINIMAL | {"rules": [_rule(match={"name": ["list_invoices"]})]})


def test_require_rejects_an_unknown_field():
    rule = _rule(
        require={
            "authorization_details": [
                {"type": "payment_initiation", "privileges": ["admin"], "bogus_field": True}
            ]
        }
    )
    with pytest.raises(ValidationError) as exc:
        PolicyConfig.model_validate(MINIMAL | {"rules": [rule]})
    assert "bogus_field" in str(exc.value)


@pytest.mark.parametrize("field_name", ["actions", "locations", "datatypes", "privileges"])
def test_require_rejects_an_empty_array_field(field_name):
    rule = _rule(
        require={"authorization_details": [{"type": "payment_initiation", field_name: []}]}
    )
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(MINIMAL | {"rules": [rule]})


def test_require_rejects_an_empty_authorization_details_list():
    rule = _rule(require={"authorization_details": []})
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(MINIMAL | {"rules": [rule]})


def test_a_rule_may_omit_require_entirely_a_carry_only_rule():
    cfg = PolicyConfig.model_validate(
        MINIMAL | {"rules": [{"match": {"tags": ["billing"]}, "outbound": {"carry": True}}]}
    )
    assert cfg.rules[0].require is None
    assert cfg.rules[0].outbound.carry is True


def test_a_require_less_rule_is_fine_under_the_default_unmatched_allow():
    PolicyConfig.model_validate(MINIMAL | {"rules": [{"match": {"tags": ["billing"]}}]})


def test_a_require_less_rule_is_a_load_error_under_unmatched_deny():
    payload = MINIMAL | {
        "defaults": {"unmatched": "deny"},
        "rules": [{"match": {"tags": ["billing"]}}],
    }
    with pytest.raises(ValidationError) as exc:
        PolicyConfig.model_validate(payload)
    assert "require" in str(exc.value)


def test_a_rule_with_require_is_fine_under_unmatched_deny():
    payload = MINIMAL | {"defaults": {"unmatched": "deny"}, "rules": [_rule()]}
    PolicyConfig.model_validate(payload)


def test_version_must_be_the_string_one():
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate({"version": "2"})
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate({})
