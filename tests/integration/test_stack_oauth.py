"""Docker integration test: outbound.mode: client_credentials against a real
mock-oauth2-server IdP, with the billing mock genuinely verifying the
issued bearer token. Proves P4a's dynamic outbound mode over Docker.
"""

from pathlib import Path

import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

pytestmark = pytest.mark.integration

STACK_CONFIG = Path(__file__).resolve().parent / "fixtures" / "stack-oauth.yaml"


@pytest.mark.anyio
async def test_list_invoices_via_a_real_client_credentials_round_trip(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        result = await app.invoker.call("billing_list_invoices", {"customer_id": "cust_1"})
        assert result.is_error is False
        assert "inv_1" in result.content[0].text
    finally:
        await app.aclose()
