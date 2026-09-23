from mcp_portal.config.models import (
    Config,
    HttpBindingEntry,
    OperationEntry,
    ServerConfig,
    UpstreamConfig,
)
from mcp_portal.config.policy import PolicyConfig
from mcp_portal.configure.interaction import ScriptedPrompter
from mcp_portal.configure.reconcile import decisions_from_config, diff_operation_ids, run_reconcile
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity


def _op(op_id: str, *, path: str = "/x", method: str = "GET") -> Operation:
    effect = Effect.READ_ONLY if method == "GET" else Effect.ACTION
    return Operation(
        id=op_id,
        upstream="billing",
        name=op_id,
        title=op_id,
        description=op_id,
        group_tags=("billing",),
        effect=effect,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method=method, path=path),
    )


def _config_with(*entries: OperationEntry) -> Config:
    return Config(
        version="1",
        mode="introspect-safe",
        server=ServerConfig(name="s", transport="stdio"),
        upstreams={"billing": UpstreamConfig(base_url="https://api.example.com")},
        operations=list(entries),
    )


def test_decisions_from_config_reconstructs_exposure_effect_and_sensitivity():
    entry = OperationEntry(
        id="list_invoices",
        upstream="billing",
        description="List invoices.",
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.SENSITIVE,
        binding=HttpBindingEntry(method="GET", path="/invoices"),
    )
    decisions = decisions_from_config(_config_with(entry), None)

    assert decisions["list_invoices"].exposed is True
    assert decisions["list_invoices"].sensitivity is Sensitivity.SENSITIVE


def test_diff_classifies_new_removed_changed_and_unchanged():
    previous = decisions_from_config(
        _config_with(
            OperationEntry(
                id="list_invoices",
                upstream="billing",
                description="d",
                effect=Effect.READ_ONLY,
                sensitivity=Sensitivity.NORMAL,
                binding=HttpBindingEntry(method="GET", path="/invoices"),
            ),
            OperationEntry(
                id="cancel_invoice",
                upstream="billing",
                description="d",
                effect=Effect.ACTION,
                sensitivity=Sensitivity.NORMAL,
                binding=HttpBindingEntry(method="POST", path="/invoices/cancel"),
            ),
        ),
        None,
    )
    current = [
        _op("list_invoices", path="/invoices"),  # unchanged
        _op("cancel_invoice", path="/invoices/cancel/v2"),  # binding changed
        _op("get_invoice", path="/invoices/{id}"),  # new
    ]

    report = diff_operation_ids(previous, current)

    assert report.new == ("get_invoice",)
    assert report.changed == ("cancel_invoice",)
    assert report.unchanged == ("list_invoices",)
    assert report.removed == ()  # every previous id is present in current


def test_removed_operations_are_reported_never_silently_dropped():
    previous = decisions_from_config(
        _config_with(
            OperationEntry(
                id="get_invoice_audit",
                upstream="billing",
                description="d",
                effect=Effect.READ_ONLY,
                sensitivity=Sensitivity.NORMAL,
                binding=HttpBindingEntry(method="GET", path="/invoices/{id}/audit"),
            )
        ),
        None,
    )
    report = diff_operation_ids(previous, [])
    assert report.removed == ("get_invoice_audit",)


def test_run_reconcile_only_surveys_new_and_changed_operations():
    previous = decisions_from_config(
        _config_with(
            OperationEntry(
                id="list_invoices",
                upstream="billing",
                description="d",
                effect=Effect.READ_ONLY,
                sensitivity=Sensitivity.NORMAL,
                binding=HttpBindingEntry(method="GET", path="/invoices"),
            )
        ),
        None,
    )
    current = [
        _op("list_invoices", path="/invoices"),  # unchanged: no prompt needed
        _op("get_invoice", path="/invoices/{id}"),  # new: one group-level prompt
    ]
    # Only one confirm scripted: if the unchanged operation were re-surveyed
    # this would run out of script and fall back to the default, masking
    # the bug, so also assert the unchanged decision equals the recorded one.
    prompter = ScriptedPrompter(confirms=[True], texts=[])

    result, report = run_reconcile(current, previous, prompter)

    by_id = {d.operation.id: d for d in result.decisions}
    assert by_id["list_invoices"] == previous["list_invoices"]
    assert by_id["get_invoice"].exposed is True
    assert report.unchanged == ("list_invoices",)
    assert report.new == ("get_invoice",)


def test_decisions_from_config_preserves_parameters_and_body_for_unchanged_detection():
    entry = OperationEntry(
        id="create_invoice",
        upstream="billing",
        description="d",
        effect=Effect.ACTION,
        sensitivity=Sensitivity.NORMAL,
        binding=HttpBindingEntry(
            method="POST",
            path="/invoices",
            body={"schema": {"type": "object", "properties": {"amount": {"type": "integer"}}}},
        ),
    )
    previous = decisions_from_config(_config_with(entry), None)

    from mcp_portal.operations import BodySpec, HttpBinding

    current = Operation(
        id="create_invoice",
        upstream="billing",
        name="create_invoice",
        title="create_invoice",
        description="create_invoice",
        group_tags=(),
        effect=Effect.ACTION,
        sensitivity=Sensitivity.NORMAL,
        input_schema={},
        binding=HttpBinding(
            method="POST",
            path="/invoices",
            body=BodySpec(
                content_type="application/json",
                schema={"type": "object", "properties": {"amount": {"type": "integer"}}},
            ),
        ),
    )

    report = diff_operation_ids(previous, [current])
    assert report.unchanged == ("create_invoice",)
    assert report.changed == ()


def test_decisions_from_config_reconstructs_require_from_a_tag_matched_policy_rule():
    from mcp_portal.config.policy import (
        AuthorizationDetailRequirement,
        PolicyMatchSpec,
        PolicyRule,
        RequireConfig,
    )

    entry = OperationEntry(
        id="create_invoice",
        upstream="billing",
        description="d",
        effect=Effect.ACTION,
        sensitivity=Sensitivity.NORMAL,
        group_tags=["billing"],
        binding=HttpBindingEntry(method="POST", path="/invoices"),
    )
    policy = PolicyConfig(
        version="1",
        rules=[
            PolicyRule(
                match=PolicyMatchSpec(tags=["billing"]),
                require=RequireConfig(
                    authorization_details=[
                        AuthorizationDetailRequirement(
                            type="payment_initiation", actions=["initiate"]
                        )
                    ]
                ),
            )
        ],
    )
    decisions = decisions_from_config(_config_with(entry), policy)

    assert decisions["create_invoice"].require is not None
    assert decisions["create_invoice"].require.type == "payment_initiation"
    assert decisions["create_invoice"].require.actions == ("initiate",)


def test_a_parameter_schema_change_is_classified_as_changed():
    """A type change is exactly the kind of drift re-confirmation exists for:
    the pinned schema is what the model is shown, so a stale one is a silent
    contract mismatch rather than a loud failure."""
    from mcp_portal.config.models import ParameterEntry
    from mcp_portal.operations import Parameter, ParamLocation

    entry = OperationEntry(
        id="get_invoice",
        upstream="billing",
        description="Get an invoice.",
        binding=HttpBindingEntry(
            method="GET",
            path="/invoices/{id}",
            parameters=[
                ParameterEntry(
                    arg="id", location=ParamLocation.PATH, required=True, schema={"type": "string"}
                )
            ],
        ),
    )
    previous = decisions_from_config(_config_with(entry), None)

    current = Operation(
        id="get_invoice",
        upstream="billing",
        name="get_invoice",
        title="get_invoice",
        description="Get an invoice.",
        group_tags=("billing",),
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={},
        binding=HttpBinding(
            method="GET",
            path="/invoices/{id}",
            parameters=(
                Parameter(
                    arg="id",
                    location=ParamLocation.PATH,
                    wire_name="id",
                    required=True,
                    schema={"type": "integer"},
                ),
            ),
        ),
    )

    assert diff_operation_ids(previous, [current]).changed == ("get_invoice",)


def test_a_request_body_content_type_change_is_classified_as_changed():
    from mcp_portal.config.models import BodyEntry
    from mcp_portal.operations import BodySpec

    schema = {"type": "object", "properties": {"amount": {"type": "integer"}}}
    entry = OperationEntry(
        id="create_invoice",
        upstream="billing",
        description="Create an invoice.",
        binding=HttpBindingEntry(
            method="POST",
            path="/invoices",
            body=BodyEntry(content_type="application/json", schema=schema),
        ),
    )
    previous = decisions_from_config(_config_with(entry), None)

    current = Operation(
        id="create_invoice",
        upstream="billing",
        name="create_invoice",
        title="create_invoice",
        description="Create an invoice.",
        group_tags=("billing",),
        effect=Effect.ACTION,
        sensitivity=Sensitivity.NORMAL,
        input_schema={},
        binding=HttpBinding(
            method="POST",
            path="/invoices",
            body=BodySpec(content_type="application/x-www-form-urlencoded", schema=schema),
        ),
    )

    assert diff_operation_ids(previous, [current]).changed == ("create_invoice",)
