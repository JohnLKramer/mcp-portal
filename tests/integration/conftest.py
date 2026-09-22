"""Shared Docker lifecycle for every tests/integration/*.py Docker suite.

One session-scoped stack: baking and starting all three services once
(billing-mock, orders-mock, mock-oauth2-server) means every fixture file
in this directory that calls billing-mock gets the same credential
requirement enforced in exactly one place — the miss that let
`stack.yaml` go uncredentialed after mocks/billing started requiring a
bearer token could not happen with a single shared owner.
"""

import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

MOCK_HEALTH_URLS = (
    "http://localhost:8081/healthz",
    "http://localhost:8082/healthz",
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
    raise TimeoutError("mock backends did not become healthy in time")


@pytest.fixture(scope="session")
def mock_stack() -> Iterator[None]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")

    subprocess.run(
        ["docker", "buildx", "bake", "billing-mock", "orders-mock"],
        cwd=REPO_ROOT,
        check=True,
    )
    subprocess.run(
        ["docker", "compose", "up", "-d", "billing-mock", "orders-mock", "mock-oauth2-server"],
        cwd=REPO_ROOT,
        check=True,
    )
    try:
        _wait_for_health()
        yield
    finally:
        subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=True)


@pytest.fixture(autouse=True)
def _orders_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDERS_MOCK_API_KEY", "orders-mock-test-key")


@pytest.fixture(autouse=True)
def _client_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_OAUTH2_CLIENT_SECRET", "any-non-empty-value")
