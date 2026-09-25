"""Golden fixture tests: dialect conversion, circular refs, SSRF, and path
merging exercised together against realistic OpenAPI 3.0/3.1 documents.

This is deliberately end-to-end within the source layer: `load_document`
(ref resolution + dialect conversion) feeds straight into `OpenApiSource`
(path/operation extraction, `x-mcp-*` handling, flattening), the same way
`build_app` wires them in production. Mode-posture filtering
(`introspect-safe` dropping non-read-only/sensitive operations) is a
`registry.apply_mode_posture` concern, not something `OpenApiSource` itself
applies, so the one test that exercises posture calls into `registry`
directly rather than assuming `OpenApiSource` filters by mode.
"""

from pathlib import Path

import httpx
import pytest

from mcp_portal.config.models import McpPortalConfig
from mcp_portal.operations import Operation, Sensitivity
from mcp_portal.registry import apply_mode_posture
from mcp_portal.sources.openapi import LoadedDocument, OpenApiSource, load_document
from mcp_portal.sources.refs import RefError

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"


def _load(name: str, **overrides) -> tuple[LoadedDocument, dict[str, Operation]]:
    cfg = McpPortalConfig.model_validate(
        {
            "version": "1",
            "mode": overrides.pop("mode", "introspect-safe"),
            "acknowledge_unsafe": overrides.pop("acknowledge_unsafe", False),
            "server": {"name": "s", "transport": "stdio"},
            "upstreams": {"billing": {"introspection": {"openapi": {"file": name} | overrides}}},
        }
    )
    openapi_cfg = cfg.upstreams["billing"].introspection.openapi
    loaded = load_document(openapi_cfg, FIXTURES, None, httpx.Client())
    ops = {o.id: o for o in OpenApiSource("billing", loaded, include_deprecated=openapi_cfg.include_deprecated).operations()}
    return loaded, ops


def build(name: str, **overrides) -> dict[str, Operation]:
    return _load(name, **overrides)[1]


def test_31_fixture_surfaces_the_expected_operations_excluding_deprecated_and_x_mcp_excluded():
    ops = build("billing-3.1.yaml")
    # get_invoice_audit is dropped by x-mcp-exclude; list_invoices_legacy is
    # dropped because it's deprecated and include_deprecated defaults False.
    # cancel_invoice is sensitive but OpenApiSource itself does not apply mode
    # posture (that's registry.apply_mode_posture's job), so it surfaces here.
    assert set(ops) == {"list_invoices", "create_invoice", "get_invoice", "cancel_invoice"}


def test_31_fixture_includes_deprecated_when_asked():
    ops = build("billing-3.1.yaml", include_deprecated=True)
    assert "list_invoices_legacy" in ops


def test_path_level_query_parameter_is_inherited_by_both_operations_on_the_path():
    ops = build("billing-3.1.yaml")
    # OpenAPI parameter names pass through verbatim; there is no camelCase ->
    # snake_case folding anywhere in the openapi source pipeline (confirmed
    # against tests/test_openapi_paths.py and tests/test_source_openapi.py,
    # which assert the same raw "customerId"/"id" names survive unchanged).
    assert "customerId" in ops["list_invoices"].input_schema["properties"]
    assert "customerId" in ops["create_invoice"].input_schema["properties"]


def test_x_mcp_sensitive_and_non_read_only_operations_are_dropped_by_mode_posture():
    # apply_mode_posture (registry.py), not OpenApiSource, is what enforces
    # introspect-safe's "read-only and non-sensitive only" rule.
    _, ops = _load("billing-3.1.yaml")
    assert ops["cancel_invoice"].sensitivity is Sensitivity.SENSITIVE
    assert ops["get_invoice"].sensitivity is Sensitivity.NORMAL
    postured = {op.id: op for op in apply_mode_posture(ops.values(), "introspect-safe")}
    assert "cancel_invoice" not in postured  # DELETE is idempotent_write, and also sensitive
    assert "list_invoices" in postured
    assert "get_invoice" in postured


def test_30_fixture_converts_nullable_and_boolean_exclusive_minimum():
    ops = build("billing-3.0.json")
    amount = ops["create_invoice"].input_schema["properties"]["amount"]
    assert amount["type"] == ["integer", "null"]
    assert amount["exclusiveMinimum"] == 0
    assert "minimum" not in amount


def test_30_fixture_circular_ref_becomes_an_open_object_and_warns():
    loaded, ops = _load("billing-3.0.json")
    assert ops["create_invoice"].input_schema["properties"]["parent"] == {"type": "object"}
    assert any("circular" in w and "Invoice" in w for w in loaded.warnings)


def test_external_ref_is_rejected_by_default():
    doc_path = FIXTURES / "ssrf-attempt.json"
    doc_path.write_text(
        '{"openapi": "3.1.0", "servers": [{"url": "https://api.example.com"}], '
        '"paths": {"/x": {"post": {"operationId": "x", "requestBody": {"content": '
        '{"application/json": {"schema": {"$ref": '
        '"https://evil.example.com/frag.yaml#/Foo"}}}}, "responses": {}}}}}'
    )
    try:
        with pytest.raises(RefError) as exc:
            build("ssrf-attempt.json")
        assert "disabled by default" in str(exc.value)
    finally:
        doc_path.unlink()
