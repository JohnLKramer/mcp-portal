import pytest
from pydantic import ValidationError

from mcp_portal.config.models import Config

MINIMAL: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "billing-portal", "transport": "stdio"},
    "upstreams": {"billing": {"base_url": "https://api.example.com"}},
    "operations": [
        {
            "id": "list_invoices",
            "upstream": "billing",
            "description": "List invoices.",
            "binding": {"method": "GET", "path": "/v1/invoices"},
        }
    ],
}


def test_minimal_config_validates():
    cfg = Config.model_validate(MINIMAL)
    assert cfg.mode == "configured"
    assert cfg.upstreams["billing"].timeout_ms == 30000


def test_mode_is_required_and_has_no_default():
    payload = {k: v for k, v in MINIMAL.items() if k != "mode"}
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "mode" in str(exc.value)


def test_p1_mode_enum_contains_only_configured():
    payload = MINIMAL | {"mode": "introspect-safe"}
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_unknown_version_is_rejected():
    payload = MINIMAL | {"version": "2"}
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_unknown_top_level_field_is_rejected():
    payload = MINIMAL | {"policy": {"file": "./p.yaml"}}
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "policy" in str(exc.value)


def test_literal_secret_is_rejected():
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {"outbound": {"mode": "static", "value": "sk-live-abc123"}},
            }
        }
    }
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "value" in str(exc.value)


def test_secret_reference_is_accepted():
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {"outbound": {"mode": "static", "value": "${env:BILLING_KEY}"}},
            }
        }
    }
    cfg = Config.model_validate(payload)
    outbound = cfg.upstreams["billing"].auth.outbound
    assert outbound.value == "${env:BILLING_KEY}"
    assert outbound.header == "Authorization"
    assert outbound.scheme == "Bearer"


def test_static_mode_requires_a_value():
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {"outbound": {"mode": "static"}},
            }
        }
    }
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_operation_referencing_an_unknown_upstream_is_rejected():
    payload = MINIMAL | {
        "operations": [
            {
                "id": "x",
                "upstream": "nope",
                "description": "d",
                "binding": {"method": "GET", "path": "/x"},
            }
        ]
    }
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "nope" in str(exc.value)
