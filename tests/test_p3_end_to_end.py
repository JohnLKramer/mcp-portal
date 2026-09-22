from pathlib import Path

import httpx
import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.anyio
async def test_the_action_operation_is_allowed_when_the_local_principal_covers_it():
    loaded = load_config(FIXTURES / "p3-config.yaml")
    app = build_app(loaded)
    for transport in app.invoker._transports.values():
        transport._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
        )
    result = await app.invoker.call("initiate_payment", {"amount": 100})
    assert result.is_error is False
    await app.aclose()


@pytest.mark.anyio
async def test_the_read_only_operation_is_unaffected_by_the_action_only_rule():
    loaded = load_config(FIXTURES / "p3-config.yaml")
    app = build_app(loaded)
    for transport in app.invoker._transports.values():
        transport._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        )
    result = await app.invoker.call("list_invoices", {})
    assert result.is_error is False
    await app.aclose()


@pytest.mark.anyio
async def test_removing_the_local_principals_authorization_detail_denies_the_action():
    raw_config = (
        (FIXTURES / "p3-config.yaml")
        .read_text()
        .replace(
            "        actions: [initiate]\n        locations:"
            ' ["https://api.example.com/v1/payments"]\n',
            "",
        )
    )
    config_path = FIXTURES / "p3-config-no-principal-detail.yaml"
    config_path.write_text(raw_config)
    try:
        loaded = load_config(config_path)
        app = build_app(loaded)
        result = await app.invoker.call("initiate_payment", {"amount": 100})
        assert result.is_error is True
        assert "payment_initiation" in result.content[0].text
        await app.aclose()
    finally:
        config_path.unlink()
