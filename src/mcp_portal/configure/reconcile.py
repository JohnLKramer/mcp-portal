"""Reconcile a fresh introspection against a previous `configure` run
(§7 of the design): new operations are surveyed, removed ones are flagged
(never silently dropped — `ReconcileReport.removed` is always printed by
the CLI before the confirm-and-write step in Task 7), changed input
schemas are re-confirmed, and unchanged operations are left exactly as
recorded.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from mcp_portal.config.models import HttpBindingEntry, McpPortalConfig
from mcp_portal.config.policy import PolicyConfig
from mcp_portal.configure.decisions import OperationDecision, RequiredDetail, SurveyResult
from mcp_portal.configure.interaction import Prompter
from mcp_portal.configure.survey import run_survey
from mcp_portal.operations import BodySpec, Effect, HttpBinding, Operation, Parameter, Sensitivity

log = logging.getLogger("mcp_portal")


def _require_for(op_id: str, operation: Operation, policy: PolicyConfig | None) -> RequiredDetail | None:
    if policy is None:
        return None
    from mcp_portal.policy import _accumulated_details, _matching_rules

    matching = _matching_rules(operation, policy.rules)
    accumulated = _accumulated_details(matching)
    if not accumulated:
        return None
    first = accumulated[0]
    return RequiredDetail(type=first.type, actions=first.actions)


def decisions_from_config(config: McpPortalConfig, policy: PolicyConfig | None) -> dict[str, OperationDecision]:
    """Reconstruct the decision that must have produced each recorded
    `OperationEntry`. Every currently-configured operation is, by
    construction, exposed — an excluded operation is simply absent from
    `operations[]`."""
    decisions: dict[str, OperationDecision] = {}
    for entry in config.operations:
        binding = entry.binding
        if not isinstance(binding, HttpBindingEntry):
            log.warning(
                "operation %r: skipping reconcile — %s bindings are not yet supported here",
                entry.id,
                type(binding).__name__,
            )
            continue
        parameters = tuple(
            Parameter(
                arg=p.arg,
                location=p.location,
                wire_name=p.wire_name or p.arg,
                required=p.required,
                schema=p.schema_,
                style=p.style,
                explode=p.explode,
            )
            for p in binding.parameters
        )
        body = (
            BodySpec(
                content_type=binding.body.content_type,
                schema=binding.body.schema_,
                mode=binding.body.mode,
            )
            if binding.body is not None
            else None
        )
        operation = Operation(
            id=entry.id,
            upstream=entry.upstream,
            # `""` means "generate a name", which is what an entry with no
            # explicit `name` asked for. Falling back to `entry.id` here
            # would let a reconcile pass pin the id as an explicit tool name
            # and quietly override `naming.strategy`.
            name=entry.name or "",
            title=entry.title or entry.description,
            description=entry.description,
            group_tags=tuple(entry.group_tags),
            effect=entry.effect or _fallback_effect(binding.method),
            sensitivity=entry.sensitivity or Sensitivity.NORMAL,
            input_schema={},
            binding=HttpBinding(method=binding.method.upper(), path=binding.path, parameters=parameters, body=body),
        )
        decisions[entry.id] = OperationDecision(
            operation=operation,
            exposed=True,
            effect=operation.effect,
            sensitivity=operation.sensitivity,
            require=_require_for(entry.id, operation, policy),
        )
    return decisions


def _fallback_effect(method: str) -> Effect:
    from mcp_portal.classify import effect_for_method

    return effect_for_method(method)


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    new: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()


def _binding_key(binding: object) -> tuple[object, ...]:
    assert isinstance(binding, HttpBinding)
    return (
        binding.method,
        binding.path,
        tuple((p.arg, p.location, p.wire_name, p.required, p.schema, p.style, p.explode) for p in binding.parameters),
        (binding.body.content_type, binding.body.mode, binding.body.schema) if binding.body else None,
    )


def diff_operation_ids(previous: dict[str, OperationDecision], current: Sequence[Operation]) -> ReconcileReport:
    current_by_id = {op.id: op for op in current}

    new = tuple(sorted(set(current_by_id) - set(previous)))
    removed = tuple(sorted(set(previous) - set(current_by_id)))

    changed: list[str] = []
    unchanged: list[str] = []
    for op_id in sorted(set(previous) & set(current_by_id)):
        if _binding_key(previous[op_id].operation.binding) == _binding_key(current_by_id[op_id].binding):
            unchanged.append(op_id)
        else:
            changed.append(op_id)

    return ReconcileReport(new=new, removed=removed, changed=tuple(changed), unchanged=tuple(unchanged))


def run_reconcile(
    current: Sequence[Operation],
    previous: dict[str, OperationDecision],
    prompter: Prompter,
) -> tuple[SurveyResult, ReconcileReport]:
    report = diff_operation_ids(previous, current)
    current_by_id = {op.id: op for op in current}

    unchanged_decisions = [previous[op_id] for op_id in report.unchanged]

    to_survey = [current_by_id[op_id] for op_id in (*report.new, *report.changed)]
    survey_defaults = {op_id: previous[op_id] for op_id in report.changed if op_id in previous}
    surveyed = run_survey(to_survey, prompter, defaults=survey_defaults) if to_survey else SurveyResult(decisions=())

    combined = tuple(unchanged_decisions) + surveyed.decisions
    return SurveyResult(decisions=combined, warnings=surveyed.warnings), report
