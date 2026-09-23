from mcp_portal.configure.decisions import OperationDecision, RequiredDetail, SurveyResult
from mcp_portal.operations import (
    Effect,
    HttpBinding,
    Operation,
    Sensitivity,
)


def _op(op_id: str = "list_invoices") -> Operation:
    return Operation(
        id=op_id,
        upstream="billing",
        name=op_id,
        title="List invoices",
        description="List invoices.",
        group_tags=("billing",),
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method="GET", path="/invoices"),
    )


def test_operation_decision_holds_the_confirmed_fields():
    decision = OperationDecision(
        operation=_op(),
        exposed=True,
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.SENSITIVE,
        require=RequiredDetail(type="payment_initiation", actions=("initiate",)),
    )
    assert decision.operation.id == "list_invoices"
    assert decision.sensitivity is Sensitivity.SENSITIVE
    assert decision.require is not None
    assert decision.require.actions == ("initiate",)


def test_survey_result_is_a_tuple_of_decisions_plus_warnings():
    result = SurveyResult(
        decisions=(
            OperationDecision(
                operation=_op(),
                exposed=False,
                effect=Effect.READ_ONLY,
                sensitivity=Sensitivity.NORMAL,
                require=None,
            ),
        ),
        warnings=("no operations matched tag 'internal'",),
    )
    assert len(result.decisions) == 1
    assert result.warnings == ("no operations matched tag 'internal'",)
