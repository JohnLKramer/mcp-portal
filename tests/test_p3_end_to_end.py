import shutil
from pathlib import Path

import httpx
import pytest
from ruamel.yaml import YAML

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

FIXTURES = Path(__file__).parent / "fixtures"

_yaml = YAML()


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
async def test_removing_the_local_principals_authorization_detail_denies_the_action(tmp_path):
    with (FIXTURES / "p3-config.yaml").open() as f:
        config_dict = _yaml.load(f)
    config_dict["auth"]["local_principal"]["authorization_details"] = []

    config_path = tmp_path / "p3-config.yaml"
    with config_path.open("w") as f:
        _yaml.dump(config_dict, f)
    shutil.copy(FIXTURES / "p3-policy.yaml", tmp_path / "p3-policy.yaml")

    loaded = load_config(config_path)
    app = build_app(loaded)
    result = await app.invoker.call("initiate_payment", {"amount": 100})
    assert result.is_error is True
    assert "payment_initiation" in result.content[0].text
    await app.aclose()
