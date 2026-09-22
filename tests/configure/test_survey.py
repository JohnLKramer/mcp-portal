from mcp_portal.configure.interaction import ScriptedPrompter
from mcp_portal.configure.survey import run_survey
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity


def _op(op_id: str, *, tags: tuple[str, ...], method: str = "GET") -> Operation:
    effect = Effect.READ_ONLY if method == "GET" else Effect.ACTION
    return Operation(
        id=op_id,
        upstream="billing",
        name=op_id,
        title=op_id,
        description=op_id,
        group_tags=tags,
        effect=effect,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method=method, path=f"/{op_id}"),
    )


def test_group_level_yes_exposes_every_operation_in_the_tag_without_further_prompts():
    ops = [_op("list_invoices", tags=("billing",)), _op("get_invoice", tags=("billing",))]
    # One confirm: the group-level "expose all 2 operations in 'billing'?".
    # No per-operation prompts follow because the group answer was yes.
    prompter = ScriptedPrompter(confirms=[True], texts=[])
    result = run_survey(ops, prompter)

    assert {d.operation.id for d in result.decisions if d.exposed} == {
        "list_invoices",
        "get_invoice",
    }


def test_group_level_no_falls_through_to_per_operation_prompts():
    ops = [_op("list_invoices", tags=("billing",)), _op("get_invoice", tags=("billing",))]
    # Group-level "no", then per-operation: expose list_invoices (yes),
    # mark list_invoices sensitive (no), expose get_invoice (no) — get_invoice
    # is not exposed so it never reaches the sensitivity prompt.
    prompter = ScriptedPrompter(confirms=[False, True, False, False], texts=[])
    result = run_survey(ops, prompter)

    by_id = {d.operation.id: d for d in result.decisions}
    assert by_id["list_invoices"].exposed is True
    assert by_id["get_invoice"].exposed is False


def test_action_effect_operations_prompt_for_a_required_rar_detail():
    ops = [_op("create_invoice", tags=("billing",), method="POST")]
    # Group-level no, expose: yes, mark sensitive: no, then a confirm to
    # attach a RAR requirement, then its type.
    prompter = ScriptedPrompter(confirms=[False, True, False, True], texts=["payment_initiation"])
    result = run_survey(ops, prompter)

    decision = result.decisions[0]
    assert decision.exposed is True
    assert decision.require is not None
    assert decision.require.type == "payment_initiation"


def test_read_only_operations_are_never_asked_for_a_rar_requirement():
    ops = [_op("list_invoices", tags=("billing",))]
    prompter = ScriptedPrompter(confirms=[False, True, False], texts=[])
    result = run_survey(ops, prompter)

    assert result.decisions[0].require is None


def test_defaults_prefill_exposed_and_require_for_a_reconciled_operation():
    from mcp_portal.configure.decisions import OperationDecision, RequiredDetail

    op = _op("create_invoice", tags=("billing",), method="POST")
    previous = OperationDecision(
        operation=op,
        exposed=True,
        effect=Effect.ACTION,
        sensitivity=Sensitivity.NORMAL,
        require=RequiredDetail(type="payment_initiation", actions=("initiate",)),
    )
    # Every confirm/text call hits the exhausted-script default fallback,
    # so the previous decision's values must be what "default" resolves to.
    prompter = ScriptedPrompter(confirms=[], texts=[])
    result = run_survey([op], prompter, defaults={"create_invoice": previous})

    decision = result.decisions[0]
    assert decision.exposed is True
    assert decision.require == previous.require
