import json
import logging
import shutil
from pathlib import Path

import httpx
import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"

CONFIG: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "billing-portal", "transport": "stdio"},
    "upstreams": {
        "billing": {
            "base_url": "https://api.example.com",
            "auth": {"outbound": {"mode": "static", "value": "${env:BILLING_KEY}"}},
        }
    },
    "naming": {"prefix_with_group_tag": True},
    "operations": [
        {
            "id": "list_invoices",
            "upstream": "billing",
            "description": "List invoices.",
            "group_tags": ["billing"],
            "binding": {
                "method": "GET",
                "path": "/v1/invoices",
                "parameters": [{"arg": "customer_id", "in": "query", "required": True}],
            },
        },
        {
            "id": "probe",
            "upstream": "billing",
            "description": "Probe.",
            "binding": {"method": "HEAD", "path": "/v1/ping"},
        },
    ],
}


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BILLING_KEY", "sk-test")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG))
    return path


def _write_config(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


@pytest.mark.anyio
async def test_app_exposes_configured_tools(config_path: Path):
    app = build_app(load_config(config_path))
    try:
        names = [t.name for t in app.invoker.tools()]
        assert names == ["billing_list_invoices"]
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_head_operations_are_dropped_end_to_end(config_path: Path):
    app = build_app(load_config(config_path))
    try:
        assert all("probe" not in t.name for t in app.invoker.tools())
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_tool_schema_reaches_the_client(config_path: Path):
    app = build_app(load_config(config_path))
    try:
        (tool,) = app.invoker.tools()
        assert tool.input_schema["required"] == ["customer_id"]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_the_configured_credential_reaches_the_outbound_request(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"invoices": []})

    # build_app owns its clients, so the mock transport is injected at construction.
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    app = build_app(load_config(config_path))
    try:
        result = await app.invoker.call("billing_list_invoices", {"customer_id": "cus_1"})
    finally:
        await app.aclose()

    assert result.is_error is False
    # `${env:BILLING_KEY}` resolved to "sk-test" and the Bearer scheme was applied.
    assert seen[0].headers["authorization"] == "Bearer sk-test"


@pytest.mark.anyio
async def test_build_app_introspects_and_serves_openapi_operations(tmp_path: Path):
    shutil.copy(FIXTURES / "billing.json", tmp_path / "billing.json")
    config_path = _write_config(
        tmp_path,
        {
            "version": "1",
            "mode": "introspect-safe",
            "server": {"name": "s", "transport": "stdio"},
            "upstreams": {"billing": {"introspection": {"openapi": {"file": "billing.json"}}}},
        },
    )
    app = build_app(load_config(config_path))
    try:
        names = {t.name for t in app.invoker.tools()}
        assert "list_invoices" in names
        assert "cancel_invoice" not in names  # DELETE isn't read_only; introspect-safe drops it
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_build_app_merges_an_explicit_override_by_id(tmp_path: Path):
    shutil.copy(FIXTURES / "billing.json", tmp_path / "billing.json")
    config_path = _write_config(
        tmp_path,
        {
            "version": "1",
            "mode": "introspect-safe",
            "server": {"name": "s", "transport": "stdio"},
            "upstreams": {"billing": {"introspection": {"openapi": {"file": "billing.json"}}}},
            "operations": [
                {
                    "id": "list_invoices",
                    "upstream": "billing",
                    "description": "Overridden description.",
                    "binding": {"method": "GET", "path": "/invoices"},
                }
            ],
        },
    )
    app = build_app(load_config(config_path))
    try:
        tool = next(t for t in app.invoker.tools() if t.name == "list_invoices")
        assert tool.description == "Overridden description."
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_build_app_logs_a_banner_for_introspect_unsafe(tmp_path: Path, caplog):
    shutil.copy(FIXTURES / "billing.json", tmp_path / "billing.json")
    config_path = _write_config(
        tmp_path,
        {
            "version": "1",
            "mode": "introspect-unsafe",
            "acknowledge_unsafe": True,
            "server": {"name": "s", "transport": "stdio"},
            "upstreams": {"billing": {"introspection": {"openapi": {"file": "billing.json"}}}},
        },
    )
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        app = build_app(load_config(config_path))
    try:
        names = {t.name for t in app.invoker.tools()}
        assert "cancel_invoice" in names  # unsafe mode keeps the DELETE
        assert "cancel_invoice" in caplog.text
        assert "introspect-unsafe" in caplog.text
    finally:
        await app.aclose()
