"""Docker integration test: RAR policy allow/deny against a live backend.
Proves P3's enforcement — `tests/test_p3_end_to_end.py` already proves the
same shape purely in-process; this proves it reaches a real HTTP call (or
is stopped before one, for the denied case).
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
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MOCK_HEALTH_URL = "http://localhost:8081/healthz"


def _wait_for_health(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(MOCK_HEALTH_URL, timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError("billing mock did not become healthy in time")


@pytest.fixture(scope="session")
def mock_stack() -> Iterator[None]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")

    subprocess.run(["docker", "buildx", "bake", "billing-mock"], cwd=REPO_ROOT, check=True)
    subprocess.run(["docker", "compose", "up", "-d", "billing-mock"], cwd=REPO_ROOT, check=True)
    try:
        _wait_for_health()
        yield
    finally:
        subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=True)


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
