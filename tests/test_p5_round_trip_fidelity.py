"""Round-trip fidelity of a `configure` write (final-review fix wave).

Every one of these guards a field or rule that a partial reconstruction of
the config/policy file used to destroy: the whole feature writes files an
operator hand-maintains, so anything `configure` doesn't understand has to
come back out byte-for-byte meaningful.
"""

import asyncio
import json
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from mcp_portal.app import build_app
from mcp_portal.cli import main
from mcp_portal.config.loader import load_config

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "openapi"
_yaml = YAML(typ="safe")

BILLING_DOC = (FIXTURE_DIR / "billing-3.1.yaml").as_posix()
ALL_IDS = {"list_invoices", "create_invoice", "get_invoice", "cancel_invoice"}


def _load(path: Path):
    if path.suffix == ".json":
        return json.loads(path.read_text())
    return _yaml.load(path.read_text())


def _responder(monkeypatch: pytest.MonkeyPatch, answer) -> None:
    """Drive `StdinPrompter` by inspecting the prompt text rather than by
    counting prompts, so a test states which operations it declines instead
    of encoding survey.py's internal prompt ordering."""

    def _input(prompt: str = "") -> str:
        return answer(prompt)

    monkeypatch.setattr("builtins.input", _input)


def _always_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    _responder(monkeypatch, lambda _prompt: "y")


def _rich_config_text() -> str:
    return f"""
version: "1"
mode: introspect-safe
server:
  name: billing-sidecar
  transport: stdio
upstreams:
  billing:
    timeout_ms: 5000
    auth:
      outbound:
        mode: static
        value: "${{env:SOME_TOKEN}}"
    introspection:
      openapi:
        file: "{BILLING_DOC}"
selection:
  exclude_ids: ["nope_not_a_real_operation"]
naming:
  strategy: method_path
classification:
  - match:
      ids: ["list_invoices"]
    sensitivity: sensitive
"""


def test_a_rich_config_survives_a_configure_run_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_TOKEN", "s3cret")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_rich_config_text())
    policy_path = tmp_path / "policy.yaml"

    _always_yes(monkeypatch)
    assert main(["configure", "--config", str(config_path), "--policy", str(policy_path)]) == 0

    reloaded = load_config(config_path)
    billing = reloaded.config.upstreams["billing"]
    assert billing.auth.outbound.mode == "static"
    assert billing.auth.outbound.value == "${env:SOME_TOKEN}"
    assert billing.timeout_ms == 5000
    assert billing.introspection is not None
    assert reloaded.config.selection.exclude_ids == ["nope_not_a_real_operation"]
    assert reloaded.config.naming.strategy == "method_path"
    assert len(reloaded.config.classification) == 1
    assert reloaded.config.classification[0].match.ids == ["list_invoices"]
    assert reloaded.config.classification[0].sensitivity is not None
    # And the operations were still written.
    assert {op.id for op in reloaded.config.operations} == ALL_IDS


def test_a_hand_authored_policy_rule_survives_alongside_a_configure_authored_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
version: "1"
mode: introspect-safe
server:
  name: billing-sidecar
  transport: stdio
upstreams:
  billing:
    introspection:
      openapi:
        file: "{BILLING_DOC}"
policy:
  file: policy.yaml
""")
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("""
version: "1"
rules:
  - match:
      tags: ["billing"]
    require:
      authorization_details:
        - type: hand_authored
          actions: ["read"]
""")

    # Decline the group accept-all so each operation is surveyed, then attach
    # a RAR requirement to `create_invoice` (the only non-read_only operation
    # we answer "y" for) so configure writes an id-matched rule of its own.
    def _answer(prompt: str) -> str:
        if prompt.startswith("expose all"):
            return "n"
        if "required detail type?" in prompt:
            return "configure_authored"
        if "require a RAR" in prompt:
            return "y" if "'create_invoice'" in prompt else "n"
        return "y"

    _responder(monkeypatch, _answer)
    assert main(["configure", "--config", str(config_path), "--policy", str(policy_path)]) == 0

    written = _load(policy_path)
    by_type = {
        rule["require"]["authorization_details"][0]["type"]: rule
        for rule in written["rules"]
        if rule.get("require")
    }
    assert "hand_authored" in by_type, written
    assert by_type["hand_authored"]["match"]["tags"] == ["billing"]
    assert by_type["hand_authored"]["require"]["authorization_details"][0]["actions"] == ["read"]
    assert "configure_authored" in by_type, written
    assert by_type["configure_authored"]["match"]["ids"] == ["create_invoice"]


def test_a_deny_default_is_not_silently_reset_to_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
version: "1"
mode: introspect-safe
server:
  name: billing-sidecar
  transport: stdio
upstreams:
  billing:
    introspection:
      openapi:
        file: "{BILLING_DOC}"
policy:
  file: policy.yaml
""")
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("""
version: "1"
defaults:
  unmatched: deny
rules:
  - match:
      tags: ["billing"]
    require:
      authorization_details:
        - type: hand_authored
""")

    _always_yes(monkeypatch)
    assert main(["configure", "--config", str(config_path), "--policy", str(policy_path)]) == 0

    assert _load(policy_path)["defaults"]["unmatched"] == "deny"


def test_configure_always_writes_mode_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
version: "1"
mode: introspect-safe
server:
  name: billing-sidecar
  transport: stdio
upstreams:
  billing:
    introspection:
      openapi:
        file: "{BILLING_DOC}"
""")
    policy_path = tmp_path / "policy.yaml"

    _always_yes(monkeypatch)
    assert main(["configure", "--config", str(config_path), "--policy", str(policy_path)]) == 0

    assert load_config(config_path).config.mode == "configured"


def test_declined_operations_are_not_served_by_the_built_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
version: "1"
mode: introspect-safe
server:
  name: billing-sidecar
  transport: stdio
upstreams:
  billing:
    introspection:
      openapi:
        file: "{BILLING_DOC}"
""")
    policy_path = tmp_path / "policy.yaml"

    declined = {"cancel_invoice", "get_invoice"}

    def _answer(prompt: str) -> str:
        if prompt.startswith("expose all"):
            return "n"
        if prompt.strip().startswith("expose '"):
            return "n" if any(f"'{op_id}'" in prompt for op_id in declined) else "y"
        if "require a RAR" in prompt:
            return "n"
        return "y"

    _responder(monkeypatch, _answer)
    assert main(["configure", "--config", str(config_path), "--policy", str(policy_path)]) == 0

    reloaded = load_config(config_path)
    assert {op.id for op in reloaded.config.operations} == ALL_IDS - declined

    # The written mode has to make that stick at serve time, not just in the
    # file: under an introspect-* mode the declined operations come straight
    # back via re-introspection.
    app = build_app(reloaded)
    try:
        served = {tool.name for tool in app.invoker.tools()}
    finally:
        asyncio.run(app.aclose())
    assert served == {"list_invoices", "create_invoice"}


def test_a_json_config_round_trips_through_configure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_TOKEN", "s3cret")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_yaml.load(_rich_config_text()), indent=2))
    policy_path = tmp_path / "policy.json"

    _always_yes(monkeypatch)
    assert main(["configure", "--config", str(config_path), "--policy", str(policy_path)]) == 0

    reloaded = load_config(config_path)
    assert reloaded.config.mode == "configured"
    assert reloaded.config.upstreams["billing"].auth.outbound.mode == "static"
    assert reloaded.config.upstreams["billing"].timeout_ms == 5000
    assert reloaded.config.selection.exclude_ids == ["nope_not_a_real_operation"]
    assert reloaded.config.naming.strategy == "method_path"
    assert len(reloaded.config.classification) == 1
    assert {op.id for op in reloaded.config.operations} == ALL_IDS


def test_a_lowercase_binding_method_reconciles_as_unchanged(tmp_path: Path) -> None:
    """A hand-edited config may spell the method `get`; introspection always
    yields `GET`. Without normalization every operation is "changed" on every
    reconcile run, forever."""
    from mcp_portal.config.models import McpPortalConfig
    from mcp_portal.configure.decisions import OperationDecision
    from mcp_portal.configure.reconcile import decisions_from_config, diff_operation_ids
    from mcp_portal.configure.write import render_config
    from mcp_portal.introspect import introspect_upstreams

    bootstrap = tmp_path / "config.yaml"
    bootstrap.write_text(f"""
version: "1"
mode: introspect-safe
server:
  name: billing-sidecar
  transport: stdio
upstreams:
  billing:
    introspection:
      openapi:
        file: "{BILLING_DOC}"
""")
    loaded = load_config(bootstrap)
    operations, base_urls = introspect_upstreams(loaded.config, loaded.base_dir)
    decisions = [
        OperationDecision(
            operation=op,
            exposed=True,
            effect=op.effect,
            sensitivity=op.sensitivity,
            require=None,
        )
        for op in operations
    ]
    rendered = render_config(decisions, server_name="billing-sidecar", upstream_base_urls=base_urls)
    for entry in rendered["operations"]:
        entry["binding"]["method"] = entry["binding"]["method"].lower()

    previous = decisions_from_config(McpPortalConfig.model_validate(rendered), None)
    report = diff_operation_ids(previous, operations)

    assert report.changed == ()
    assert report.new == ()
    assert report.removed == ()
    assert set(report.unchanged) == {op.id for op in operations}


def test_a_second_no_op_configure_run_leaves_the_file_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
version: "1"
mode: introspect-safe
server:
  name: billing-sidecar
  transport: stdio
upstreams:
  billing:
    introspection:
      openapi:
        file: "{BILLING_DOC}"
""")
    policy_path = tmp_path / "policy.yaml"

    _always_yes(monkeypatch)
    assert main(["configure", "--config", str(config_path), "--policy", str(policy_path)]) == 0
    first = config_path.read_text()

    _always_yes(monkeypatch)
    assert main(["configure", "--config", str(config_path), "--policy", str(policy_path)]) == 0

    assert config_path.read_text() == first
