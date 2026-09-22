"""Docker integration test: introspect-safe mode against a live OpenAPI
document served by the billing mock. Proves P2's live-introspection flow —
`tests/test_openapi_golden.py` already proves the parser against static
fixtures; this proves the whole path against a real HTTP GET.
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
STACK_CONFIG = Path(__file__).resolve().parent / "fixtures" / "stack-introspection.yaml"
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
