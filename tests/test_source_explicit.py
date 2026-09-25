import logging

import pytest

from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import McpPortalConfig
from mcp_portal.operations import Effect, Sensitivity
from mcp_portal.sources.explicit import ExplicitSource

BASE: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "s", "transport": "stdio"},
    "upstreams": {"billing": {"base_url": "https://api.example.com"}},
}


def build(operations: list[dict]) -> list:
    config = McpPortalConfig.model_validate(BASE | {"operations": operations})
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


def test_head_operations_are_never_exposed_even_with_an_explicit_effect():
    assert (
        build(
            [
                {
                    "id": "probe",
                    "upstream": "billing",
                    "description": "Probe.",
                    "effect": "read_only",
                    "binding": {"method": "HEAD", "path": "/v1/x"},
                }
            ]
        )
        == []
    )


def test_flatten_failure_surfaces_as_a_config_error():
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


def entry(binding: dict, op_id: str = "get") -> dict:
    return {"id": op_id, "upstream": "billing", "description": "Get.", "binding": binding}


def test_a_path_placeholder_with_no_parameter_is_a_config_error():
    with pytest.raises(ConfigError) as exc:
        build([entry({"method": "GET", "path": "/v1/invoices/{id}"})])
    assert "'id'" in str(exc.value)


def test_a_path_parameter_with_no_placeholder_is_a_config_error():
    with pytest.raises(ConfigError) as exc:
        build(
            [
                entry(
                    {
                        "method": "GET",
                        "path": "/v1/invoices",
                        "parameters": [
                            {"arg": "invoice_id", "in": "path", "wire_name": "id", "required": True}
                        ],
                    }
                )
            ]
        )
    assert "invoice_id" in str(exc.value)


def test_an_optional_path_parameter_is_a_config_error():
    with pytest.raises(ConfigError) as exc:
        build(
            [
                entry(
                    {
                        "method": "GET",
                        "path": "/v1/invoices/{id}",
                        "parameters": [
                            {
                                "arg": "invoice_id",
                                "in": "path",
                                "wire_name": "id",
                                "required": False,
                            }
                        ],
                    }
                )
            ]
        )
    assert "invoice_id" in str(exc.value)
    assert "{id}" in str(exc.value)


def test_a_repeated_path_placeholder_is_a_config_error():
    with pytest.raises(ConfigError) as exc:
        build(
            [
                entry(
                    {
                        "method": "GET",
                        "path": "/v1/{id}/child/{id}",
                        "parameters": [{"arg": "id", "in": "path", "required": True}],
                    }
                )
            ]
        )
    assert "id" in str(exc.value)


def test_two_path_parameters_sharing_a_wire_name_are_a_config_error():
    with pytest.raises(ConfigError) as exc:
        build(
            [
                entry(
                    {
                        "method": "GET",
                        "path": "/v1/invoices/{id}",
                        "parameters": [
                            {"arg": "a", "in": "path", "wire_name": "id", "required": True},
                            {"arg": "b", "in": "path", "wire_name": "id", "required": True},
                        ],
                    }
                )
            ]
        )
    message = str(exc.value)
    assert "'a'" in message
    assert "'b'" in message
    assert "'id'" in message


def test_a_matching_path_template_and_parameter_load_cleanly():
    (op,) = build(
        [
            entry(
                {
                    "method": "GET",
                    "path": "/v1/invoices/{id}",
                    "parameters": [
                        {"arg": "invoice_id", "in": "path", "wire_name": "id", "required": True}
                    ],
                }
            )
        ]
    )
    assert op.binding.path == "/v1/invoices/{id}"


def body_entry(schema: dict) -> dict:
    return entry(
        {
            "method": "POST",
            "path": "/v1/invoices",
            "body": {"content_type": "application/json", "mode": "flatten", "schema": schema},
        },
        op_id="create",
    )


PLAIN_BODY: dict = {
    "type": "object",
    "properties": {"amount": {"type": "integer"}},
    "required": ["amount"],
}


def test_a_flattened_body_warns_about_constraints_it_cannot_carry(caplog):
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        build([body_entry(PLAIN_BODY | {"additionalProperties": False})])
    assert "additionalProperties" in caplog.text
    assert "create" in caplog.text


def test_a_plain_flattened_body_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        build([body_entry(PLAIN_BODY)])
    assert caplog.text == ""


def test_a_single_arg_body_keeps_its_constraints_and_does_not_warn(caplog):
    payload = body_entry(PLAIN_BODY | {"oneOf": [{"required": ["amount"]}]})
    payload["binding"]["body"]["mode"] = "single_arg"
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        (op,) = build([payload])
    assert caplog.text == ""
    assert "oneOf" in op.input_schema["properties"]["body"]
