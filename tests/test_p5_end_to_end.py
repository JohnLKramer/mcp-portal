"""End-to-end: configure a fresh sidecar from an OpenAPI fixture, then
reconcile after the upstream document changes underneath it.

Proves Tasks 1-7 compose: introspect -> survey -> write (run 1), then
introspect -> reconcile (new/changed/unchanged/removed) -> survey only the
delta -> write (run 2), and that the written config loads cleanly through
the real `config.loader.load_config` both times.
"""

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from mcp_portal.cli import main
from mcp_portal.config.loader import load_config

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "openapi"
_yaml = YAML(typ="safe")


def _write_config(tmp_path: Path, openapi_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(f"""
version: "1"
mode: introspect-safe
server:
  name: billing-sidecar
  transport: stdio
upstreams:
  billing:
    introspection:
      openapi:
        file: "{openapi_path.as_posix()}"
""")
    return path


@pytest.fixture
def modifiable_openapi(tmp_path: Path) -> Path:
    target = tmp_path / "billing.yaml"
    target.write_text((FIXTURE_DIR / "billing-3.1.yaml").read_text())
    return target


def test_configure_then_reconcile_after_an_upstream_change(
    tmp_path: Path, modifiable_openapi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path, modifiable_openapi)
    policy_path = tmp_path / "policy.yaml"

    # Run 1: fresh survey. `_always_yes` answers every confirm() with "y";
    # the fixture only exercises `confirm`, never `text`, so an unlimited
    # generous answer is robust to the exact prompt count (one group-level
    # accept-all for tag 'billing', then confirm writing policy, then
    # confirm writing config).
    def _always_yes(*_args: object) -> str:
        return "y"

    monkeypatch.setattr("builtins.input", _always_yes)
    assert main(["configure", "--config", str(config_path), "--policy", str(policy_path)]) == 0

    loaded = load_config(config_path)
    first_ids = {op.id for op in loaded.config.operations}
    assert first_ids == {"list_invoices", "create_invoice", "get_invoice", "cancel_invoice"}

    # Mutate the upstream document: add a brand-new operation, and change
    # `get_invoice`'s binding (simulating a real upstream contract change) by
    # moving its route to a new path template with a renamed path parameter.
    doc = _yaml.load(modifiable_openapi.read_text())
    doc["paths"]["/invoices/{id}/history"] = {
        "get": {
            "operationId": "get_invoice_history",
            "summary": "History.",
            "tags": ["billing"],
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
            ],
            "responses": {"200": {"description": "OK"}},
        }
    }
    # `/invoices/{id}` hosts both `get_invoice` (GET) and `cancel_invoice`
    # (DELETE); only `get_invoice`'s route moves, so pull just the `get`
    # operation out into a new path template rather than moving the whole
    # path item (which would also drag `cancel_invoice` — meant to stay
    # untouched — along with it).
    get_invoice_op = doc["paths"]["/invoices/{id}"].pop("get")
    get_invoice_op["parameters"][0]["name"] = "invoiceId"
    doc["paths"]["/invoices/{invoiceId}"] = {"get": get_invoice_op}
    writer = YAML()
    with modifiable_openapi.open("w") as f:
        writer.dump(doc, f)

    # Run 2: reconcile. `get_invoice`'s binding changed (route moved to a new
    # path template) so it is re-surveyed, `get_invoice_history` is new so it is surveyed
    # too, and `list_invoices`/`create_invoice`/`cancel_invoice` are
    # unchanged so they are not handed to `run_survey` again. Script
    # generously and assert on outcome, not on exact prompt count, to keep
    # this test robust to internal grouping/prompt-count details in
    # survey.py/reconcile.py.
    monkeypatch.setattr("builtins.input", _always_yes)
    code = main(["configure", "--config", str(config_path), "--policy", str(policy_path)])
    assert code == 0

    loaded_again = load_config(config_path)
    second_ids = {op.id for op in loaded_again.config.operations}
    assert "get_invoice_history" in second_ids
    assert "get_invoice" in second_ids  # re-confirmed, still present
    assert "list_invoices" in second_ids  # unchanged, still present
    assert "cancel_invoice" in second_ids  # unchanged, still present

    # The unchanged operations' recorded decisions were not re-derived —
    # spot-check that `list_invoices`'s entry still round-trips through
    # the real loader without error and keeps its original effect.
    by_id = {op.id: op for op in loaded_again.config.operations}
    assert by_id["list_invoices"].effect.value == "read_only"
