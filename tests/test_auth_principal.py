import pytest
from mcp.server.auth.provider import AccessToken

from mcp_portal.auth.principal import Principal, local_principal, principal_from_access_token
from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import LocalPrincipalConfig


def test_default_local_principal_has_no_authorization_details():
    principal = local_principal(LocalPrincipalConfig())
    assert isinstance(principal, Principal)
    assert principal.identity == "local"
    assert principal.authorization_details == ()


def test_local_principal_parses_configured_authorization_details():
    config = LocalPrincipalConfig(
        authorization_details=[
            {"type": "payment_initiation", "actions": ["initiate"], "identifier": "acct-1"}
        ]
    )
    principal = local_principal(config)
    (detail,) = principal.authorization_details
    assert detail.type == "payment_initiation"
    assert detail.actions == ("initiate",)
    assert detail.identifier == "acct-1"


def test_a_malformed_authorization_detail_is_a_startup_config_error():
    # Missing 'type' — parse_authorization_details raises RarError, which a
    # config-sourced principal must surface as ConfigError: every
    # configuration problem is a load error, never a call-time surprise (§10).
    config = LocalPrincipalConfig(authorization_details=[{"actions": ["initiate"]}])
    with pytest.raises(ConfigError) as exc:
        local_principal(config)
    assert "type" in str(exc.value)


def _access_token(**overrides) -> AccessToken:
    base = dict(token="raw-jwt", client_id="c", scopes=["invoices.read"], claims={})
    return AccessToken(**(base | overrides))


def test_local_principal_has_no_subject_token():
    principal = local_principal(LocalPrincipalConfig())
    assert principal.subject_token is None


def test_principal_from_access_token_carries_the_raw_token_as_subject_token():
    principal = principal_from_access_token(_access_token())
    assert principal is not None
    assert principal.subject_token == "raw-jwt"


def test_principal_from_access_token_carries_the_expiry():
    principal = principal_from_access_token(_access_token(expires_at=1_700_000_000))
    assert principal is not None
    assert principal.subject_token_expires_at == 1_700_000_000


def test_local_principal_has_no_subject_token_expiry():
    principal = local_principal(LocalPrincipalConfig())
    assert principal.subject_token_expires_at is None


def test_principal_from_access_token_parses_authorization_details_from_claims():
    access = _access_token(
        claims={"authorization_details": [{"type": "payment_initiation", "actions": ["initiate"]}]}
    )
    principal = principal_from_access_token(access)
    assert principal is not None
    (detail,) = principal.authorization_details
    assert detail.type == "payment_initiation"


def test_principal_from_access_token_defaults_to_no_authorization_details():
    principal = principal_from_access_token(_access_token())
    assert principal is not None
    assert principal.authorization_details == ()


def test_principal_from_access_token_returns_none_for_a_malformed_claim():
    access = _access_token(
        claims={"authorization_details": [{"actions": ["initiate"]}]}
    )  # missing 'type'
    assert principal_from_access_token(access) is None


def test_principal_identity_prefers_subject_then_falls_back_to_client_id():
    with_subject = principal_from_access_token(_access_token(subject="user-1"))
    assert with_subject.identity == "user-1"
    without_subject = principal_from_access_token(_access_token(subject=None))
    assert without_subject.identity == "c"
