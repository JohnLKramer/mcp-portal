"""Docker integration test: the `configure` CLI against a live OpenAPI
document served by the billing mock. Proves P5's whole authoring path
reaches a real HTTP introspection call and produces a config that a real
`build_app` can serve real calls through — `tests/test_p5_end_to_end.py`
and `tests/test_p5_round_trip_fidelity.py` already prove the survey/
reconcile/render logic against static fixtures; this proves the same path
end to end against the live mock stack, the way
`test_stack_introspection.py` does for plain introspection.

Writes its config into `tmp_path`, never into `tests/integration/fixtures/`
— `configure` overwrites the file it's pointed at, and a fixture under
version control must not be mutated by a test run.
"""

import json
from pathlib import Path

import pytest

from mcp_portal.app import build_app
from mcp_portal.cli import main
from mcp_portal.config.loader import load_config

pytestmark = pytest.mark.integration


def _write_config(path: Path) -> None:
    path.write_text("""
version: "1"
mode: introspect-safe

server:
  name: integration-portal-configure
  transport: stdio

upstreams:
  billing:
    base_url: http://localhost:8081
    timeout_ms: 5000
    auth:
      outbound:
        mode: client_credentials
        token_endpoint: http://localhost:8083/default/token
        client_id: billing-client
        client_secret: ${env:MOCK_OAUTH2_CLIENT_SECRET}
    introspection:
      openapi:
        url: http://localhost:8081/openapi.json
""")


@pytest.mark.anyio
async def test_configure_against_the_live_mock_produces_a_servable_config(
    mock_stack: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config_path = tmp_path / "config.yaml"
    policy_path = tmp_path / "policy.yaml"
    _write_config(config_path)

    monkeypatch.setattr("builtins.input", lambda *_args: "y")
    code = main(["configure", "--config", str(config_path), "--policy", str(policy_path)])
    assert code == 0

    loaded = load_config(config_path)
    # The live document only declares listInvoices/createInvoice (§ mock's
    # own _OPENAPI_DOC); the survey's group-level accept-all exposes both.
    ids = {op.id for op in loaded.config.operations}
    assert ids == {"listInvoices", "createInvoice"}
    # configure always forces mode: configured (§7) regardless of the
    # introspect-safe mode the bootstrap config started from.
    assert loaded.config.mode == "configured"
    # The upstream's introspection pointer and outbound auth must have
    # survived the write untouched, or a future `configure` run (and this
    # very `build_app` call below) would have nothing to work with.
    assert loaded.config.upstreams["billing"].introspection is not None
    assert loaded.config.upstreams["billing"].auth.outbound.mode == "client_credentials"

    app = build_app(loaded)
    try:
        names = {t.name for t in app.invoker.tools()}
        assert "listinvoices" in names
        assert "createinvoice" in names

        result = await app.invoker.call("listinvoices", {"customerId": "cust_1"})
        assert result.is_error is False
        assert "inv_1" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_a_second_configure_run_reconciles_instead_of_rediscovering_from_scratch(
    mock_stack: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config_path = tmp_path / "config.yaml"
    policy_path = tmp_path / "policy.yaml"
    _write_config(config_path)

    monkeypatch.setattr("builtins.input", lambda *_args: "y")
    assert main(["configure", "--config", str(config_path), "--policy", str(policy_path)]) == 0
    first_ids = {op.id for op in load_config(config_path).config.operations}

    # Re-running against the now-`configured` file must still be able to
    # re-introspect (the introspection pointer must have survived the first
    # write) and must reconcile rather than losing every operation, which is
    # exactly the bug the final review caught and this suite guards against
    # at the live-stack level.
    code = main(["configure", "--config", str(config_path), "--policy", str(policy_path)])
    assert code == 0

    second_config = json.loads(json.dumps(load_config(config_path).config.model_dump(mode="json")))
    second_ids = {op["id"] for op in second_config["operations"]}
    assert second_ids == first_ids
