from pathlib import Path

import httpx
import pytest

from mcp_portal.config.loader import ConfigError, load_config
from mcp_portal.config.models import McpPortalConfig
from mcp_portal.introspect import introspect_upstreams

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "openapi"

_SCHEMA_RESPONSE = {
    "data": {
        "__schema": {
            "queryType": {"name": "Query"},
            "mutationType": None,
            "types": [
                {
                    "kind": "OBJECT",
                    "name": "Query",
                    "fields": [
                        {
                            "name": "ping",
                            "args": [],
                            "type": {"kind": "SCALAR", "name": "String", "ofType": None},
                        }
                    ],
                }
            ],
        }
    }
}

_GRAPHQL_CONFIG = {
    "version": "1",
    "mode": "introspect-safe",
    "server": {"name": "test", "transport": "stdio"},
    "upstreams": {
        "gql": {
            "base_url": "https://api.example.com/graphql",
            "introspection": {"graphql": {"url": "https://api.example.com/graphql"}},
        }
    },
}


def test_introspect_upstreams_returns_operations_and_base_urls(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
version: "1"
mode: introspect-safe
server:
  name: s
  transport: stdio
upstreams:
  billing:
    introspection:
      openapi:
        file: "{(FIXTURE_DIR / "billing-3.1.yaml").as_posix()}"
""")
    loaded = load_config(config_path)
    operations, base_urls = introspect_upstreams(loaded.config, loaded.base_dir)

    ids = {op.id for op in operations}
    assert "list_invoices" in ids
    assert "get_invoice_audit" not in ids  # x-mcp-exclude
    assert base_urls["billing"] == "https://api.example.com/v1"


def test_introspect_upstreams_builds_graphql_operations(tmp_path, monkeypatch):
    def fake_client_post(self, url, json=None, **kwargs):
        return httpx.Response(200, json=_SCHEMA_RESPONSE, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", fake_client_post)
    config = McpPortalConfig.model_validate(_GRAPHQL_CONFIG)
    ops, base_urls = introspect_upstreams(config, tmp_path)
    assert {op.id for op in ops} == {"ping"}
    assert base_urls["gql"] == "https://api.example.com/graphql"


def test_introspect_upstreams_wraps_a_graphql_introspection_failure(tmp_path, monkeypatch):
    def fake_client_post(self, url, json=None, **kwargs):
        return httpx.Response(200, json={"errors": [{"message": "nope"}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", fake_client_post)
    config = McpPortalConfig.model_validate(_GRAPHQL_CONFIG)
    with pytest.raises(ConfigError, match="nope"):
        introspect_upstreams(config, tmp_path)
