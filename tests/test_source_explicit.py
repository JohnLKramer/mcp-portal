import pytest

from mcp_portal.config.models import Config
from mcp_portal.operations import Effect, Sensitivity
from mcp_portal.sources.explicit import ExplicitSource

BASE: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "s", "transport": "stdio"},
    "upstreams": {"billing": {"base_url": "https://api.example.com"}},
}


def build(operations: list[dict]) -> list:
    config = Config.model_validate(BASE | {"operations": operations})
    return list(ExplicitSource(config).operations())


def test_entry_becomes_an_operation_with_derived_effect():
    (op,) = build(
        [
            {
                "id": "list",
                "upstream": "billing",
                "description": "List.",
                "binding": {"method": "GET", "path": "/v1/invoices"},
            }
        ]
    )
    assert op.id == "list"
    assert op.effect is Effect.READ_ONLY
    assert op.sensitivity is Sensitivity.NORMAL
    assert op.name == ""


def test_explicit_effect_overrides_the_derived_one():
    (op,) = build(
        [
            {
                "id": "reindex",
                "upstream": "billing",
                "description": "Reindex.",
                "effect": "action",
                "binding": {"method": "PUT", "path": "/v1/reindex"},
            }
        ]
    )
    assert op.effect is Effect.ACTION


def test_title_defaults_to_the_first_line_of_the_description():
    (op,) = build(
        [
            {
                "id": "list",
                "upstream": "billing",
                "description": "List invoices.\nMore detail.",
                "binding": {"method": "GET", "path": "/v1/invoices"},
            }
        ]
    )
    assert op.title == "List invoices."


def test_input_schema_is_synthesized_from_the_binding():
    (op,) = build(
        [
            {
                "id": "get",
                "upstream": "billing",
                "description": "Get.",
                "binding": {
                    "method": "GET",
                    "path": "/v1/invoices/{id}",
                    "parameters": [
                        {"arg": "invoice_id", "in": "path", "wire_name": "id", "required": True},
                    ],
                },
            }
        ]
    )
    assert op.input_schema["properties"]["invoice_id"] == {"type": "string"}
    assert op.input_schema["required"] == ["invoice_id"]


def test_wire_name_defaults_to_the_argument_name():
    (op,) = build(
        [
            {
                "id": "get",
                "upstream": "billing",
                "description": "Get.",
                "binding": {
                    "method": "GET",
                    "path": "/v1/x",
                    "parameters": [{"arg": "cursor", "in": "query"}],
                },
            }
        ]
    )
    assert op.binding.parameters[0].wire_name == "cursor"


def test_head_operations_are_never_exposed():
    assert (
        build(
            [
                {
                    "id": "probe",
                    "upstream": "billing",
                    "description": "Probe.",
                    "binding": {"method": "HEAD", "path": "/v1/x"},
                }
            ]
        )
        == []
    )


def test_flatten_failure_surfaces_as_a_config_error():
    from mcp_portal.config.loader import ConfigError

    with pytest.raises(ConfigError):
        build(
            [
                {
                    "id": "up",
                    "upstream": "billing",
                    "description": "Upload.",
                    "binding": {
                        "method": "POST",
                        "path": "/v1/up",
                        "body": {
                            "content_type": "multipart/form-data",
                            "schema": {"type": "object"},
                        },
                    },
                }
            ]
        )
