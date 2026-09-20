"""End-to-end integration test: drives mcp-portal against live mock backends.

Requires Docker. Excluded from the default `uv run pytest` run — invoke with
`uv run pytest -m integration`. Skips (not fails) if Docker isn't available.
"""

import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
STACK_CONFIG = Path(__file__).resolve().parent / "fixtures" / "stack.yaml"
MOCK_HEALTH_URLS = ("http://localhost:8081/healthz", "http://localhost:8082/healthz")


def _wait_for_health(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if all(httpx.get(url, timeout=1.0).status_code == 200 for url in MOCK_HEALTH_URLS):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError("mock backends did not become healthy in time")


@pytest.fixture(scope="session")
def mock_stack() -> Iterator[None]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")

    subprocess.run(
        ["docker", "compose", "up", "-d", "--build", "billing-mock", "orders-mock"],
        cwd=REPO_ROOT,
        check=True,
    )
    try:
        _wait_for_health()
        yield
    finally:
        subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=True)


@pytest.mark.anyio
async def test_list_invoices_round_trips_through_the_billing_mock(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        result = await app.invoker.call("billing_list_invoices", {"customer_id": "cust_1"})
        assert result.is_error is False
        assert "inv_1" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_get_customer_tax_id_round_trips_through_the_billing_mock(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        result = await app.invoker.call("billing_get_customer_tax_id", {"customer_id": "cust_1"})
        assert result.is_error is False
        assert "TAX-CUST-1" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_create_invoice_round_trips_through_the_billing_mock(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        result = await app.invoker.call(
            "billing_create_invoice", {"customer_id": "cust_3", "amount_cents": 500}
        )
        assert result.is_error is False
        assert "cust_3" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_list_orders_round_trips_through_the_orders_mock(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        result = await app.invoker.call("orders_list_orders", {})
        assert result.is_error is False
        assert "ord_1" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_get_order_round_trips_through_the_orders_mock(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        result = await app.invoker.call("orders_get_order", {"order_id": "ord_1"})
        assert result.is_error is False
        assert "cust_1" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_create_order_round_trips_through_the_orders_mock(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        result = await app.invoker.call(
            "orders_create_order", {"customer_id": "cust_4", "item_count": 2}
        )
        assert result.is_error is False
        assert "cust_4" in result.content[0].text
    finally:
        await app.aclose()
