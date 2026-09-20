import json
from pathlib import Path

import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

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
