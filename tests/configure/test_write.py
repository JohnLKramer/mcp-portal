import json
from pathlib import Path

from mcp_portal.configure.decisions import OperationDecision, RequiredDetail
from mcp_portal.configure.interaction import ScriptedPrompter
from mcp_portal.configure.write import render_config, render_policy, write_with_confirmation
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity


def _decision(op_id: str, *, exposed: bool = True, require: RequiredDetail | None = None):
    op = Operation(
        id=op_id,
        upstream="billing",
        name=op_id,
        title=op_id,
        description=f"{op_id} description",
        group_tags=("billing",),
        effect=Effect.READ_ONLY if require is None else Effect.ACTION,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method="GET" if require is None else "POST", path=f"/{op_id}"),
    )
    return OperationDecision(
        operation=op,
        exposed=exposed,
        effect=op.effect,
        sensitivity=Sensitivity.NORMAL,
        require=require,
    )


def test_render_config_includes_only_exposed_operations_as_pinned_entries():
    decisions = [_decision("list_invoices"), _decision("get_invoice_audit", exposed=False)]
    rendered = render_config(
        decisions,
        server_name="billing-sidecar",
        upstream_base_urls={"billing": "https://api.example.com"},
    )

    ids = {entry["id"] for entry in rendered["operations"]}
    assert ids == {"list_invoices"}
    assert rendered["operations"][0]["effect"] == "read_only"
    assert rendered["upstreams"]["billing"]["base_url"] == "https://api.example.com"
    assert rendered["server"]["name"] == "billing-sidecar"
    assert rendered["mode"] == "configured"
    assert rendered["version"] == "1"


def test_render_policy_adds_a_rule_only_for_operations_with_a_required_detail():
    decisions = [
        _decision("list_invoices"),
        _decision(
            "create_invoice",
            require=RequiredDetail(type="payment_initiation", actions=("initiate",)),
        ),
    ]
    policy = render_policy(decisions)

    assert policy["version"] == "1"
    assert len(policy["rules"]) == 1
    rule = policy["rules"][0]
    assert rule["match"]["ids"] == ["create_invoice"]
    assert rule["require"]["authorization_details"][0]["type"] == "payment_initiation"
    assert rule["require"]["authorization_details"][0]["actions"] == ["initiate"]


def test_render_policy_regenerates_a_simple_type_and_actions_only_rule():
    from mcp_portal.config.policy import AuthorizationDetailRequirement as PolicyAuthDetail
    from mcp_portal.config.policy import (
        PolicyConfig,
        PolicyMatchSpec,
        PolicyRule,
        RequireConfig,
    )

    simple_rule = PolicyRule(
        match=PolicyMatchSpec(ids=["create_invoice"]),
        require=RequireConfig(
            authorization_details=[
                PolicyAuthDetail(type="payment_initiation", actions=["initiate"])
            ]
        ),
    )
    existing_policy = PolicyConfig(version="1", rules=[simple_rule])

    decision = _decision(
        "create_invoice",
        require=RequiredDetail(type="payment_initiation", actions=("initiate", "confirm")),
    )
    rendered = render_policy([decision], existing_policy=existing_policy)

    assert len(rendered["rules"]) == 1
    rule = rendered["rules"][0]
    assert rule["require"]["authorization_details"][0]["actions"] == ["initiate", "confirm"]


def test_render_policy_preserves_a_rich_id_matched_rule_configure_cannot_fully_represent():
    from mcp_portal.config.policy import (
        AuthorizationDetailRequirement,
        PolicyConfig,
        PolicyMatchSpec,
        PolicyOutboundConfig,
        PolicyRule,
        RequireConfig,
    )

    rich_rule = PolicyRule(
        match=PolicyMatchSpec(ids=["create_invoice"]),
        outbound=PolicyOutboundConfig(carry=True),
        require=RequireConfig(
            authorization_details=[
                AuthorizationDetailRequirement(
                    type="payment_initiation",
                    actions=["initiate"],
                    locations=["https://api.example.com/v1/payments"],
                )
            ]
        ),
    )
    existing_policy = PolicyConfig(version="1", rules=[rich_rule])

    decision = _decision(
        "create_invoice", require=RequiredDetail(type="payment_initiation", actions=("initiate",))
    )
    rendered = render_policy([decision], existing_policy=existing_policy)

    # The rich rule must survive untouched — not narrowed to type+actions,
    # not stripped of outbound.carry or locations, and not duplicated by a
    # second configure-generated rule for the same operation.
    assert len(rendered["rules"]) == 1
    rule = rendered["rules"][0]
    assert rule["outbound"]["carry"] is True
    assert rule["require"]["authorization_details"][0]["locations"] == [
        "https://api.example.com/v1/payments"
    ]


def test_write_with_confirmation_writes_json_when_confirmed(tmp_path: Path):
    path = tmp_path / "config.json"
    rendered = {"version": "1", "mode": "configured"}
    prompter = ScriptedPrompter(confirms=[True], texts=[])

    wrote = write_with_confirmation(path, rendered, prompter)

    assert wrote is True
    assert json.loads(path.read_text()) == rendered


def test_write_with_confirmation_does_not_touch_disk_when_declined(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": "1", "mode": "configured", "untouched": True}))
    prompter = ScriptedPrompter(confirms=[False], texts=[])

    wrote = write_with_confirmation(path, {"version": "1", "mode": "configured"}, prompter)

    assert wrote is False
    assert json.loads(path.read_text())["untouched"] is True


def test_write_with_confirmation_round_trips_yaml_preserving_comments(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text('# a hand-written comment\nversion: "1"\nmode: configured\n')
    prompter = ScriptedPrompter(confirms=[True], texts=[])

    write_with_confirmation(
        path, {"version": "1", "mode": "configured", "extra": "field"}, prompter
    )

    text = path.read_text()
    assert "# a hand-written comment" in text
    assert "extra: field" in text


def test_render_config_preserves_parameters_and_body():
    from mcp_portal.operations import BodySpec, Parameter, ParamLocation

    op = Operation(
        id="create_invoice",
        upstream="billing",
        name="create_invoice",
        title="create_invoice",
        description="create_invoice",
        group_tags=("billing",),
        effect=Effect.ACTION,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(
            method="POST",
            path="/invoices/{id}",
            parameters=(
                Parameter(
                    arg="id",
                    location=ParamLocation.PATH,
                    wire_name="id",
                    required=True,
                    schema={"type": "string"},
                ),
            ),
            body=BodySpec(
                content_type="application/json",
                schema={"type": "object", "properties": {"amount": {"type": "integer"}}},
            ),
        ),
    )
    decision = OperationDecision(
        operation=op,
        exposed=True,
        effect=Effect.ACTION,
        sensitivity=Sensitivity.NORMAL,
        require=None,
    )
    rendered = render_config(
        [decision], server_name="s", upstream_base_urls={"billing": "https://api.example.com"}
    )

    binding = rendered["operations"][0]["binding"]
    assert binding["parameters"][0]["arg"] == "id"
    assert binding["parameters"][0]["in"] == "path"
    assert binding["parameters"][0]["required"] is True
    assert binding["body"]["schema"]["properties"]["amount"]["type"] == "integer"

    # The rendered dict must actually be loadable as a real Config.
    from mcp_portal.config.models import Config

    Config.model_validate(rendered)


def test_write_with_confirmation_creates_missing_parent_directories(tmp_path: Path):
    path = tmp_path / "nested" / "dir" / "config.json"
    prompter = ScriptedPrompter(confirms=[True], texts=[])

    wrote = write_with_confirmation(path, {"version": "1"}, prompter)

    assert wrote is True
    assert json.loads(path.read_text()) == {"version": "1"}
