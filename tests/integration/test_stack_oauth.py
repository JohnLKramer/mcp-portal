"""Docker integration test: outbound.mode: client_credentials against a real
mock-oauth2-server IdP, with the billing mock genuinely verifying the
issued bearer token. Proves P4a's dynamic outbound mode over Docker.
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
STACK_CONFIG = Path(__file__).resolve().parent / "fixtures" / "stack-oauth.yaml"
MOCK_HEALTH_URLS = (
    "http://localhost:8081/healthz",
    "http://localhost:8083/default/.well-known/openid-configuration",
)


def _wait_for_health(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if all(httpx.get(url, timeout=1.0).status_code == 200 for url in MOCK_HEALTH_URLS):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError("billing mock / mock-oauth2-server did not become healthy in time")


@pytest.fixture(scope="session")
def mock_stack() -> Iterator[None]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")

    subprocess.run(
        ["docker", "buildx", "bake", "billing-mock"], cwd=REPO_ROOT, check=True
    )
    subprocess.run(
        ["docker", "compose", "up", "-d", "billing-mock", "mock-oauth2-server"],
        cwd=REPO_ROOT,
        check=True,
    )
    try:
        _wait_for_health()
        yield
    finally:
        subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=True)


@pytest.fixture(autouse=True)
def _client_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_OAUTH2_CLIENT_SECRET", "any-non-empty-value")


@pytest.mark.anyio
async def test_list_invoices_via_a_real_client_credentials_round_trip(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        result = await app.invoker.call(
            "billing_list_invoices", {"customer_id": "cust_1"}
        )
        assert result.is_error is False
        assert "inv_1" in result.content[0].text
    finally:
        await app.aclose()
