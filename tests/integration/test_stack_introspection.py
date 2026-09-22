"""Docker integration test: introspect-safe mode against a live OpenAPI
document served by the billing mock. Proves P2's live-introspection flow —
`tests/test_openapi_golden.py` already proves the parser against static
fixtures; this proves the whole path against a real HTTP GET.
"""

from pathlib import Path

import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

pytestmark = pytest.mark.integration

STACK_CONFIG = Path(__file__).resolve().parent / "fixtures" / "stack-introspection.yaml"


@pytest.mark.anyio
async def test_list_invoices_via_live_openapi_introspection(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        names = [t.name for t in app.invoker.tools()]
        assert "listinvoices" in names

        result = await app.invoker.call("listinvoices", {"customerId": "cust_1"})
        assert result.is_error is False
        assert "inv_1" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_create_invoice_is_excluded_under_introspect_safe_mode(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        names = [t.name for t in app.invoker.tools()]
        assert "createinvoice" not in names
    finally:
        await app.aclose()
