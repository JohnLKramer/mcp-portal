"""The data a survey or reconcile pass produces, independent of how it was
gathered (interactively or replayed from a previous config) or how it will
be rendered (main config vs. policy file — see `configure/write.py`).
"""

from dataclasses import dataclass

from mcp_portal.operations import Effect, Operation, Sensitivity


@dataclass(frozen=True, slots=True)
class RequiredDetail:
    """One RFC 9396 detail an operator attached to an operation. Mirrors
    `config.policy.AuthorizationDetailRequirement`'s `type`/`actions` shape;
    kept as a separate, dependency-free dataclass here because `configure`
    must be able to represent "no requirement yet decided" (`None` on
    `OperationDecision.require`) without importing the policy file's
    Pydantic models into the survey's own domain."""

    type: str
    actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OperationDecision:
    """One operation's confirmed exposure decision.

    `effect` and `sensitivity` are carried here (not left to be re-derived
    from `operation`) because a survey answer can override either — pinning
    the confirmed value, not the introspected default, is what
    `configure/write.py` serializes into `OperationEntry.effect` /
    `.sensitivity`.
    """

    operation: Operation
    exposed: bool
    effect: Effect
    sensitivity: Sensitivity
    require: RequiredDetail | None


@dataclass(frozen=True, slots=True)
class SurveyResult:
    decisions: tuple[OperationDecision, ...]
    warnings: tuple[str, ...] = ()
