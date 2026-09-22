"""The interactive survey (§7 of the design): propose, don't interrogate.

Walks operations group tag by group tag, offering a group-level accept-all
before descending to individual operations — without this, a 200-operation
API guarantees rubber-stamping.
"""

from collections.abc import Sequence
from itertools import groupby

from mcp_portal.configure.decisions import OperationDecision, RequiredDetail, SurveyResult
from mcp_portal.configure.interaction import Prompter
from mcp_portal.operations import Effect, Operation

UNGROUPED_TAG = "(ungrouped)"


def _tag_for(op: Operation) -> str:
    return op.group_tags[0] if op.group_tags else UNGROUPED_TAG


def _default_for(op: Operation, defaults: dict[str, OperationDecision]) -> OperationDecision:
    if op.id in defaults:
        return defaults[op.id]
    return OperationDecision(
        operation=op,
        exposed=True,
        effect=op.effect,
        sensitivity=op.sensitivity,
        require=None,
    )


def _survey_one(
    op: Operation, prompter: Prompter, defaults: dict[str, OperationDecision]
) -> OperationDecision:
    default = _default_for(op, defaults)

    exposed = prompter.confirm(f"  expose {op.id!r} ({op.effect.value})?", default=default.exposed)
    if not exposed:
        return OperationDecision(
            operation=op,
            exposed=False,
            effect=op.effect,
            sensitivity=op.sensitivity,
            require=None,
        )

    # Sensitivity is carried over from the default (introspected or, on a
    # reconcile re-survey, the previously recorded decision) rather than
    # asked about here — a per-operation "mark sensitive?" prompt on top of
    # the expose/RAR prompts would double the question count for no signal
    # the group-level walk doesn't already give the operator a chance to
    # flag via the operation's own description.
    sensitivity = default.sensitivity

    require = default.require
    if op.effect is not Effect.READ_ONLY:
        attach = prompter.confirm(
            f"  require a RAR authorization_details entry to call {op.id!r}?",
            default=default.require is not None,
        )
        if attach:
            detail_type = prompter.text(
                "    required detail type?",
                default=default.require.type if default.require else "",
            )
            require = RequiredDetail(type=detail_type) if detail_type else None
        else:
            require = None
    else:
        require = None

    return OperationDecision(
        operation=op, exposed=True, effect=op.effect, sensitivity=sensitivity, require=require
    )


def run_survey(
    operations: Sequence[Operation],
    prompter: Prompter,
    *,
    defaults: dict[str, OperationDecision] | None = None,
) -> SurveyResult:
    defaults = defaults or {}
    decisions: list[OperationDecision] = []
    warnings: list[str] = []

    ordered = sorted(operations, key=_tag_for)
    for tag, group_iter in groupby(ordered, key=_tag_for):
        group = list(group_iter)
        accept_all = prompter.confirm(
            f"expose all {len(group)} operation(s) in {tag!r}?", default=True
        )
        if accept_all:
            for op in group:
                default = _default_for(op, defaults)
                decisions.append(
                    OperationDecision(
                        operation=op,
                        exposed=True,
                        effect=op.effect,
                        sensitivity=default.sensitivity,
                        require=default.require if op.effect is not Effect.READ_ONLY else None,
                    )
                )
            continue

        for op in group:
            decisions.append(_survey_one(op, prompter, defaults))

    return SurveyResult(decisions=tuple(decisions), warnings=tuple(warnings))
