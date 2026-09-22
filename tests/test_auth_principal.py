import pytest

from mcp_portal.auth.principal import Principal, local_principal
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
