"""Docker integration test: RAR policy allow/deny against a live backend.
Proves P3's enforcement — `tests/test_p3_end_to_end.py` already proves the
same shape purely in-process; this proves it reaches a real HTTP call (or
is stopped before one, for the denied case).
"""

from pathlib import Path

import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.anyio
async def test_the_policy_gated_operation_is_denied_without_the_required_detail(
    mock_stack: None,
):
    app = build_app(load_config(FIXTURES / "stack-policy-denied.yaml"))
    try:
        result = await app.invoker.call(
            "billing_get_customer_tax_id", {"customer_id": "cust_1"}
        )
        assert result.is_error is True
        assert "customer_data" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_an_unmatched_operation_is_allowed_under_the_same_policy(mock_stack: None):
    app = build_app(load_config(FIXTURES / "stack-policy-denied.yaml"))
    try:
        result = await app.invoker.call(
            "billing_list_invoices", {"customer_id": "cust_1"}
        )
        assert result.is_error is False
        assert "inv_1" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_the_policy_gated_operation_is_allowed_with_the_required_detail(
    mock_stack: None,
):
    app = build_app(load_config(FIXTURES / "stack-policy-authorized.yaml"))
    try:
        result = await app.invoker.call(
            "billing_get_customer_tax_id", {"customer_id": "cust_1"}
        )
        assert result.is_error is False
        assert "TAX-CUST-1" in result.content[0].text
    finally:
        await app.aclose()
