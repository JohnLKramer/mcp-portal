import pytest
from pydantic import ValidationError

from mcp_portal.config.models import (
    AuthConfig,
    Config,
    IntrospectionConfig,
    OutboundConfig,
    ServerConfig,
)

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


def test_mode_enum_rejects_anything_outside_the_three_published_values():
    payload = MINIMAL | {"mode": "surveil"}
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_unknown_version_is_rejected():
    payload = MINIMAL | {"version": "2"}
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_unknown_top_level_field_is_rejected():
    payload = MINIMAL | {"unknown_field": {"file": "./p.yaml"}}
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "unknown_field" in str(exc.value)


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


@pytest.mark.parametrize("name", ["List_Invoices", "list invoices", "list!", "", "a" * 65])
def test_an_explicit_tool_name_outside_the_published_pattern_is_rejected(name: str):
    payload = MINIMAL | {"operations": [MINIMAL["operations"][0] | {"name": name}]}
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "name" in str(exc.value)


def test_a_well_formed_explicit_tool_name_is_accepted():
    payload = MINIMAL | {"operations": [MINIMAL["operations"][0] | {"name": "invoices_2"}]}
    assert Config.model_validate(payload).operations[0].name == "invoices_2"


@pytest.mark.parametrize(
    "base_url", ["api.example.com", "/v1", "ftp://api.example.com", "https://"]
)
def test_a_base_url_that_is_not_an_absolute_http_url_is_rejected(base_url: str):
    payload = MINIMAL | {"upstreams": {"billing": {"base_url": base_url}}}
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "base_url" in str(exc.value)


def test_a_parameter_style_other_than_form_is_rejected():
    binding = MINIMAL["operations"][0]["binding"] | {
        "parameters": [{"arg": "ids", "in": "query", "style": "spaceDelimited"}]
    }
    payload = MINIMAL | {"operations": [MINIMAL["operations"][0] | {"binding": binding}]}
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "style" in str(exc.value)


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


def test_introspect_safe_does_not_require_acknowledgement():
    Config.model_validate(MINIMAL | {"mode": "introspect-safe"})


def test_introspect_unsafe_without_acknowledgement_is_rejected():
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(MINIMAL | {"mode": "introspect-unsafe"})
    assert "acknowledge_unsafe" in str(exc.value)


def test_introspect_unsafe_with_acknowledgement_is_accepted():
    cfg = Config.model_validate(MINIMAL | {"mode": "introspect-unsafe", "acknowledge_unsafe": True})
    assert cfg.mode == "introspect-unsafe"


def test_upstream_with_neither_base_url_nor_introspection_is_rejected():
    payload = MINIMAL | {"mode": "introspect-safe", "upstreams": {"billing": {}}}
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "base_url" in str(exc.value) or "introspection" in str(exc.value)


def test_upstream_may_rely_on_introspection_instead_of_base_url():
    payload = MINIMAL | {
        "mode": "introspect-safe",
        "upstreams": {
            "billing": {
                "introspection": {"openapi": {"url": "https://api.example.com/openapi.json"}}
            }
        },
    }
    cfg = Config.model_validate(payload)
    assert cfg.upstreams["billing"].base_url is None
    assert cfg.upstreams["billing"].introspection.openapi.url == (
        "https://api.example.com/openapi.json"
    )


def test_configured_mode_requires_base_url_even_when_introspection_is_present():
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "introspection": {"openapi": {"url": "https://api.example.com/openapi.json"}}
            }
        }
    }
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "configured" in str(exc.value)


def test_openapi_introspection_requires_exactly_one_of_url_or_file():
    def with_openapi(openapi: dict) -> dict:
        return MINIMAL | {
            "mode": "introspect-safe",
            "upstreams": {
                "billing": {
                    "base_url": "https://api.example.com",
                    "introspection": {"openapi": openapi},
                }
            },
        }

    with pytest.raises(ValidationError):
        Config.model_validate(with_openapi({}))
    with pytest.raises(ValidationError):
        Config.model_validate(
            with_openapi({"url": "https://api.example.com/openapi.json", "file": "./openapi.json"})
        )


def test_openapi_introspection_defaults():
    payload = MINIMAL | {
        "mode": "introspect-safe",
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "introspection": {"openapi": {"file": "./openapi.json"}},
            }
        },
    }
    openapi = Config.model_validate(payload).upstreams["billing"].introspection.openapi
    assert openapi.include_deprecated is False
    assert openapi.allow_external_refs is False
    assert openapi.allowed_hosts == []


def test_auth_and_policy_are_both_optional():
    cfg = Config.model_validate(MINIMAL)
    assert cfg.auth.local_principal.authorization_details == []
    assert cfg.policy is None


def test_local_principal_accepts_a_list_of_raw_authorization_details():
    payload = MINIMAL | {
        "auth": {
            "local_principal": {
                "authorization_details": [{"type": "payment_initiation", "actions": ["initiate"]}]
            }
        }
    }
    cfg = Config.model_validate(payload)
    assert cfg.auth.local_principal.authorization_details[0]["type"] == "payment_initiation"


def test_policy_file_is_a_bare_path_string():
    cfg = Config.model_validate(MINIMAL | {"policy": {"file": "./rar-policy.yaml"}})
    assert cfg.policy.file == "./rar-policy.yaml"


def test_auth_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        Config.model_validate(MINIMAL | {"auth": {"inbound": {"enabled": True}}})


def test_client_credentials_mode_requires_token_endpoint_client_id_and_secret():
    with pytest.raises(ValidationError):
        OutboundConfig(mode="client_credentials")


def test_client_credentials_mode_validates_with_all_three():
    cfg = OutboundConfig(
        mode="client_credentials",
        token_endpoint="https://idp.example.com/oauth2/token",
        client_id="sidekit-billing",
        client_secret="${env:BILLING_CLIENT_SECRET}",
        scopes=["invoices.write"],
    )
    assert cfg.token_endpoint == "https://idp.example.com/oauth2/token"
    assert cfg.scopes == ["invoices.write"]


def test_client_credentials_mode_rejects_a_literal_client_secret():
    with pytest.raises(ValidationError):
        OutboundConfig(
            mode="client_credentials",
            token_endpoint="https://idp.example.com/oauth2/token",
            client_id="sidekit-billing",
            client_secret="not-a-reference",
        )


def test_transport_http_defaults_its_own_server_config():
    cfg = ServerConfig(name="s", transport="http")
    assert cfg.http.host == "127.0.0.1"
    assert cfg.http.path == "/mcp"
    assert cfg.http.allowed_origins == []
    # Empty by default: a loopback bind needs no operator-supplied Host, since
    # `_security_settings` allows the loopback spellings itself.
    assert cfg.http.allowed_hosts == []


def test_inbound_disabled_by_default():
    cfg = Config.model_validate(MINIMAL)
    assert cfg.auth.inbound.enabled is False


def test_inbound_enabled_requires_issuer_and_audience():
    with pytest.raises(ValidationError):
        AuthConfig(inbound={"enabled": True})


def test_inbound_enabled_validates_with_issuer_and_audience():
    cfg = AuthConfig(
        inbound={
            "enabled": True,
            "issuer": "https://idp.example.com",
            "audience": "https://api.example.com/mcp",
        }
    )
    assert cfg.inbound.algorithms == ["RS256", "ES256"]
    assert cfg.inbound.leeway_s == 60.0


def test_inbound_rejects_none_in_algorithms():
    with pytest.raises(ValidationError):
        AuthConfig(
            inbound={
                "enabled": True,
                "issuer": "https://idp.example.com",
                "audience": "https://api.example.com/mcp",
                "algorithms": ["none"],
            }
        )


def test_http_transport_on_a_non_loopback_host_without_inbound_is_a_startup_error():
    payload = MINIMAL | {"server": {"name": "s", "transport": "http", "http": {"host": "0.0.0.0"}}}
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_http_transport_on_a_non_loopback_host_with_the_escape_hatch_is_allowed():
    payload = MINIMAL | {
        "server": {"name": "s", "transport": "http", "http": {"host": "0.0.0.0"}},
        "auth": {"inbound": {"allow_unauthenticated_http": True}},
    }
    Config.model_validate(payload)  # does not raise


def test_http_transport_on_loopback_without_inbound_is_allowed():
    payload = MINIMAL | {"server": {"name": "s", "transport": "http"}}
    Config.model_validate(payload)  # does not raise


def test_token_exchange_under_stdio_is_a_startup_error():
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {
                    "outbound": {
                        "mode": "token_exchange",
                        "token_endpoint": "https://idp.example.com/oauth2/token",
                        "client_id": "c",
                        "client_secret": "${env:S}",
                    }
                },
            }
        }
    }
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_token_exchange_with_inbound_disabled_over_http_is_a_startup_error():
    payload = MINIMAL | {
        "server": {"name": "s", "transport": "http"},
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {
                    "outbound": {
                        "mode": "token_exchange",
                        "token_endpoint": "https://idp.example.com/oauth2/token",
                        "client_id": "c",
                        "client_secret": "${env:S}",
                    }
                },
            }
        },
    }
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_introspection_requires_exactly_one_of_openapi_or_graphql():
    with pytest.raises(ValidationError):
        IntrospectionConfig.model_validate({})
    with pytest.raises(ValidationError):
        IntrospectionConfig.model_validate(
            {
                "openapi": {"url": "https://api.example.com/openapi.json"},
                "graphql": {"url": "https://api.example.com/graphql"},
            }
        )
    cfg = IntrospectionConfig.model_validate(
        {"graphql": {"url": "https://api.example.com/graphql"}}
    )
    assert cfg.graphql is not None
    assert cfg.graphql.type_policy == {}


def test_graphql_binding_entry_validates():
    payload = MINIMAL | {
        "upstreams": {
            "gql": {
                "base_url": "https://api.example.com",
            }
        },
        "operations": [
            {
                "id": "get_user",
                "upstream": "gql",
                "description": "Fetch a user by id.",
                "binding": {
                    "protocol": "graphql",
                    "operation_type": "query",
                    "document": "query GetUser($id: ID!) { user(id: $id) { id name } }",
                    "variables": [{"name": "id", "graphql_type": "ID!", "required": True}],
                },
            }
        ],
    }
    cfg = Config.model_validate(payload)
    assert cfg.operations[0].binding.protocol == "graphql"


def test_binding_entry_rejects_an_unknown_protocol():
    payload = MINIMAL | {
        "operations": [
            {
                "id": "x",
                "upstream": "billing",
                "description": "d",
                "binding": {"protocol": "grpc", "method": "GET", "path": "/x"},
            }
        ]
    }
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_token_exchange_with_http_and_inbound_enabled_is_valid():
    payload = MINIMAL | {
        "server": {"name": "s", "transport": "http"},
        "auth": {
            "inbound": {
                "enabled": True,
                "issuer": "https://idp.example.com",
                "audience": "https://api.example.com/mcp",
            }
        },
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {
                    "outbound": {
                        "mode": "token_exchange",
                        "token_endpoint": "https://idp.example.com/oauth2/token",
                        "client_id": "c",
                        "client_secret": "${env:S}",
                        "audience": "https://api.example.com",
                    }
                },
            }
        },
    }
    Config.model_validate(payload)  # does not raise
