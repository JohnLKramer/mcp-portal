from pathlib import Path

from mcp_portal.config.loader import load_config
from mcp_portal.introspect import introspect_upstreams

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "openapi"


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
