import pytest

from mcp_portal.auth.rar import (
    AuthorizationDetail,
    RarError,
    covers,
    parse_authorization_details,
)


def detail(**overrides) -> AuthorizationDetail:
    base = {
        "type": "payment_initiation",
        "actions": (),
        "locations": (),
        "datatypes": (),
        "identifier": None,
        "privileges": (),
    }
    return AuthorizationDetail(**(base | overrides))


def test_a_detail_with_matching_type_and_no_other_fields_is_covered():
    required = detail()
    presented = detail()
    assert covers(required, [presented]) is True


def test_a_detail_with_a_different_type_is_never_covered():
    required = detail(type="payment_initiation")
    presented = detail(type="account_access")
    assert covers(required, [presented]) is False


def test_required_actions_must_be_a_subset_of_presented_actions():
    required = detail(actions=("initiate",))
    assert covers(required, [detail(actions=("initiate", "cancel"))]) is True
    assert covers(required, [detail(actions=("cancel",))]) is False


def test_required_locations_match_by_exact_string_equality():
    required = detail(locations=("https://api.example.com/v1/payments",))
    assert covers(required, [detail(locations=("https://api.example.com/v1/payments",))]) is True
    # A prefix is not equality: hierarchical matching is deliberately deferred.
    assert covers(required, [detail(locations=("https://api.example.com/v1",))]) is False


def test_required_datatypes_must_be_a_subset():
    required = detail(datatypes=("balance",))
    assert covers(required, [detail(datatypes=("balance", "history"))]) is True
    assert covers(required, [detail(datatypes=("history",))]) is False


def test_required_privileges_must_be_a_subset():
    required = detail(privileges=("admin",))
    assert covers(required, [detail(privileges=("admin", "read"))]) is True
    assert covers(required, [detail(privileges=("read",))]) is False


def test_required_identifier_must_equal_presented_identifier():
    required = detail(identifier="acct-1")
    assert covers(required, [detail(identifier="acct-1")]) is True
    assert covers(required, [detail(identifier="acct-2")]) is False


def test_a_field_the_required_detail_does_not_specify_is_not_checked():
    # Presented has extra privileges the requirement never asked about.
    required = detail(type="payment_initiation", actions=("initiate",))
    presented = detail(type="payment_initiation", actions=("initiate",), privileges=("admin",))
    assert covers(required, [presented]) is True


def test_a_field_the_required_detail_specifies_but_presented_lacks_fails_coverage():
    required = detail(identifier="acct-1")
    presented = detail(identifier=None)
    assert covers(required, [presented]) is False


def test_coverage_must_come_from_a_single_presented_detail_not_a_composition():
    required = detail(actions=("initiate",), locations=("https://api.example.com/pay",))
    presented = [
        detail(actions=("initiate",)),
        detail(locations=("https://api.example.com/pay",)),
    ]
    assert covers(required, presented) is False


def test_covers_with_no_presented_details_is_false():
    assert covers(detail(), []) is False


def test_parse_authorization_details_accepts_a_well_formed_list():
    raw = [
        {
            "type": "payment_initiation",
            "actions": ["initiate"],
            "locations": ["https://api.example.com/pay"],
            "identifier": "acct-1",
        }
    ]
    (parsed,) = parse_authorization_details(raw)
    assert parsed.type == "payment_initiation"
    assert parsed.actions == ("initiate",)
    assert parsed.locations == ("https://api.example.com/pay",)
    assert parsed.identifier == "acct-1"


def test_parse_authorization_details_defaults_missing_optional_fields():
    (parsed,) = parse_authorization_details([{"type": "account_access"}])
    assert parsed.actions == ()
    assert parsed.datatypes == ()
    assert parsed.privileges == ()
    assert parsed.identifier is None


def test_parse_authorization_details_empty_list_is_fine():
    assert parse_authorization_details([]) == ()


def test_parse_authorization_details_rejects_a_non_list():
    with pytest.raises(RarError):
        parse_authorization_details({"type": "x"})


def test_parse_authorization_details_rejects_a_non_object_entry():
    with pytest.raises(RarError):
        parse_authorization_details(["not-an-object"])


def test_parse_authorization_details_rejects_a_missing_type():
    with pytest.raises(RarError) as exc:
        parse_authorization_details([{"actions": ["initiate"]}])
    assert "type" in str(exc.value)


def test_parse_authorization_details_rejects_a_non_string_type():
    with pytest.raises(RarError):
        parse_authorization_details([{"type": 5}])


def test_parse_authorization_details_rejects_a_non_list_actions_field():
    with pytest.raises(RarError):
        parse_authorization_details([{"type": "x", "actions": "initiate"}])
