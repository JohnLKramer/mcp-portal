import pytest

from mcp_portal.auth.outbound import credential_for
from mcp_portal.config.models import OutboundConfig


def test_none_mode_yields_no_credential():
    assert credential_for(OutboundConfig(mode="none"), {}) is None


def test_static_mode_applies_the_scheme():
    cfg = OutboundConfig(mode="static", value="${env:K}")
    cred = credential_for(cfg, {"${env:K}": "sk-test"})
    assert cred is not None
    assert cred.header == "Authorization"
    assert cred.value == "Bearer sk-test"


def test_static_mode_without_a_scheme_sends_a_bare_value():
    cfg = OutboundConfig(mode="static", header="X-Api-Key", scheme=None, value="${env:K}")
    cred = credential_for(cfg, {"${env:K}": "sk-test"})
    assert cred is not None
    assert cred.header == "X-Api-Key"
    assert cred.value == "sk-test"


def test_unresolved_secret_is_an_error():
    from mcp_portal.config.loader import ConfigError

    cfg = OutboundConfig(mode="static", value="${env:MISSING}")
    with pytest.raises(ConfigError):
        credential_for(cfg, {})
