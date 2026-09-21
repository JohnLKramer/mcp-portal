import logging
from pathlib import Path

import httpx
import pytest

from mcp_portal.config.models import OpenApiIntrospectionConfig
from mcp_portal.operations import Effect, Sensitivity
from mcp_portal.sources.openapi import OpenApiSource, load_document
from mcp_portal.sources.refs import RefError

DOC = {
    "openapi": "3.1.0",
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/invoices": {
            "get": {
                "operationId": "list_invoices",
                "summary": "List invoices.",
                "tags": ["billing"],
                "responses": {},
            }
        },
        "/invoices/{id}": {
            "delete": {
                "operationId": "cancel_invoice",
                "summary": "Cancel an invoice.",
                "x-mcp-sensitive": True,
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {},
            }
        },
        "/invoices/{id}/audit": {
            "get": {
                "operationId": "get_audit",
                "summary": "Internal audit trail.",
                "x-mcp-exclude": True,
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {},
            }
        },
        "/invoices/upload": {
            "post": {
                "operationId": "bulk_upload",
                "summary": "Upload a batch file.",
                "requestBody": {"content": {"multipart/form-data": {"schema": {"type": "object"}}}},
                "responses": {},
            }
        },
    },
}


def write_doc(tmp_path: Path, doc: dict = DOC, name: str = "openapi.json") -> Path:
    import json

    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return path


def load(tmp_path: Path, doc: dict = DOC, **overrides) -> list:
    write_doc(tmp_path, doc)
    config = OpenApiIntrospectionConfig(file="openapi.json", **overrides)
    loaded = load_document(config, tmp_path, None, httpx.Client())
    return list(
        OpenApiSource("billing", loaded, include_deprecated=config.include_deprecated).operations()
    )


def test_a_get_operation_becomes_a_read_only_operation(tmp_path: Path):
    ops = {o.id: o for o in load(tmp_path)}
    assert ops["list_invoices"].effect is Effect.READ_ONLY
    assert ops["list_invoices"].upstream == "billing"
    assert ops["list_invoices"].group_tags == ("billing",)


def test_x_mcp_sensitive_marks_the_operation_sensitive(tmp_path: Path):
    ops = {o.id: o for o in load(tmp_path)}
    assert ops["cancel_invoice"].sensitivity is Sensitivity.SENSITIVE


def test_x_mcp_exclude_drops_the_operation_in_every_mode(tmp_path: Path):
    ops = {o.id: o for o in load(tmp_path)}
    assert "get_audit" not in ops


def test_an_unsupported_content_type_is_skipped_with_a_warning_not_a_hard_failure(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        ops = {o.id: o for o in load(tmp_path)}
    assert "bulk_upload" not in ops
    assert "multipart/form-data" in caplog.text


def test_input_schema_is_synthesized_from_the_binding(tmp_path: Path):
    ops = {o.id: o for o in load(tmp_path)}
    schema = ops["cancel_invoice"].input_schema
    assert schema["properties"]["id"] == {"type": "string"}
    assert schema["required"] == ["id"]


def test_title_falls_back_from_summary_to_description_to_id(tmp_path: Path):
    ops = {o.id: o for o in load(tmp_path)}
    assert ops["list_invoices"].title == "List invoices."


def test_operations_with_no_operation_id_get_a_stable_slug(tmp_path: Path):
    doc = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {"/ping": {"get": {"summary": "Ping.", "responses": {}}}},
    }
    (op,) = load(tmp_path, doc)
    assert op.id == "get_ping"


def test_x_mcp_name_sets_the_tool_name_not_the_id(tmp_path: Path):
    doc = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/ping": {"get": {"operationId": "ping", "x-mcp-name": "health_check", "responses": {}}}
        },
    }
    (op,) = load(tmp_path, doc)
    assert op.id == "ping"
    assert op.name == "health_check"


def test_an_invalid_x_mcp_name_is_dropped_with_a_warning(tmp_path, caplog):
    doc = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/ping": {"get": {"operationId": "ping", "x-mcp-name": "Not Valid!", "responses": {}}}
        },
    }
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        (op,) = load(tmp_path, doc)
    assert op.name == ""
    assert "Not Valid!" in caplog.text


def test_load_document_resolves_base_url_from_servers_when_upstream_has_none(tmp_path: Path):
    write_doc(tmp_path)
    config = OpenApiIntrospectionConfig(file="openapi.json")
    loaded = load_document(config, tmp_path, None, httpx.Client())
    assert loaded.base_url == "https://api.example.com/v1"


def test_load_document_lets_configured_base_url_override_the_document(tmp_path: Path):
    write_doc(tmp_path)
    config = OpenApiIntrospectionConfig(file="openapi.json")
    loaded = load_document(config, tmp_path, "https://override.example.com", httpx.Client())
    assert loaded.base_url == "https://override.example.com"


def test_load_document_rejects_an_external_ref_by_default(tmp_path: Path):
    doc = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/x": {
                "post": {
                    "operationId": "x",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "https://evil.example.com/frag.yaml#/Foo"}
                            }
                        }
                    },
                    "responses": {},
                }
            }
        },
    }
    write_doc(tmp_path, doc)
    config = OpenApiIntrospectionConfig(file="openapi.json")
    with pytest.raises(RefError):
        load_document(config, tmp_path, None, httpx.Client())


def test_load_document_applies_dialect_conversion_only_to_30_documents(tmp_path: Path):
    doc = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/x": {
                "post": {
                    "operationId": "x",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"amount": {"type": "integer", "nullable": True}},
                                }
                            }
                        }
                    },
                    "responses": {},
                }
            }
        },
    }
    write_doc(tmp_path, doc)
    config = OpenApiIntrospectionConfig(file="openapi.json")
    loaded = load_document(config, tmp_path, None, httpx.Client())
    (op,) = list(OpenApiSource("billing", loaded, include_deprecated=False).operations())
    assert op.input_schema["properties"]["amount"]["type"] == ["integer", "null"]


def test_an_external_ref_to_a_plain_component_fragment_resolves(tmp_path: Path):
    doc = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/x": {
                "post": {
                    "operationId": "x",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "https://trusted.example.com/common.yaml#/Foo"}
                            }
                        }
                    },
                    "responses": {},
                }
            }
        },
    }
    write_doc(tmp_path, doc)
    config = OpenApiIntrospectionConfig(
        file="openapi.json", allow_external_refs=True, allowed_hosts=["trusted.example.com"]
    )

    import mcp_portal.sources.openapi as openapi_module

    original_fetch_text = openapi_module.fetch_text

    def fetch(target: str, base_dir, client) -> tuple[str, bool]:
        # `fetch_text` is also used by `load_document` itself to read the entry
        # document ("openapi.json"), so only intercept the external ref target
        # and delegate everything else to the real implementation.
        if target == "https://trusted.example.com/common.yaml":
            return '{"Foo": {"type": "object", "properties": {"n": {"type": "integer"}}}}', False
        return original_fetch_text(target, base_dir, client)

    openapi_module.fetch_text = fetch
    try:
        loaded = load_document(config, tmp_path, None, httpx.Client())
        (op,) = list(OpenApiSource("billing", loaded, include_deprecated=False).operations())
    finally:
        openapi_module.fetch_text = original_fetch_text
    assert op.input_schema["properties"]["n"] == {"type": "integer"}


def test_a_non_string_x_mcp_description_does_not_crash_the_load(tmp_path: Path):
    doc = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {"/x": {"get": {"operationId": "x", "x-mcp-description": 42, "responses": {}}}},
    }
    (op,) = load(tmp_path, doc)
    assert op.description == "42"
