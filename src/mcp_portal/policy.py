"""Rule matching, requirement accumulation, and the allow/deny decision.

Matching reuses the same vocabulary `registry.py` uses for selection — `id`
globs, `tags`, `upstream`, `effect` — extended with `sensitivity`, which
policy alone needs (registry's own `MatchSpec` intentionally does not carry
it: sensitivity is consumed at registry-build time and settable there, not
here). There is deliberately no `match.name`, for the reason `registry.py`
already gives: names are unstable by construction.
"""

import dataclasses
from collections.abc import Sequence
from fnmatch import fnmatch

from mcp_portal.auth.principal import Principal
from mcp_portal.auth.rar import AuthorizationDetail, covers
from mcp_portal.config.policy import PolicyConfig, PolicyMatchSpec, PolicyRule
from mcp_portal.operations import Operation


@dataclasses.dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    missing: tuple[AuthorizationDetail, ...] = ()
    carry: bool = False


def _matches(op: Operation, spec: PolicyMatchSpec) -> bool:
    if spec.ids and not any(fnmatch(op.id, pattern) for pattern in spec.ids):
        return False
    if spec.tags and not set(spec.tags) & set(op.group_tags):
        return False
    if spec.upstream and op.upstream not in spec.upstream:
        return False
    if spec.effect and op.effect not in spec.effect:
        return False
    return not (spec.sensitivity and op.sensitivity not in spec.sensitivity)


def _matching_rules(op: Operation, rules: Sequence[PolicyRule]) -> list[PolicyRule]:
    return [rule for rule in rules if _matches(op, rule.match)]


class PolicyEngine:
    def __init__(self, policy: PolicyConfig) -> None:
        self._policy = policy

    def evaluate(self, operation: Operation, principal: Principal) -> PolicyDecision:
        matching = _matching_rules(operation, self._policy.rules)
        carry = any(rule.outbound.carry for rule in matching)

        # "Matched" means "contributed a requirement" (§5) — a carry-only
        # rule's match still sets `carry` above, but never makes an operation
        # matched for the purposes of `defaults.unmatched`.
        required: list[AuthorizationDetail] = []
        for rule in matching:
            if rule.require is None:
                continue
            required.extend(
                AuthorizationDetail(
                    type=d.type,
                    actions=tuple(d.actions or ()),
                    locations=tuple(d.locations or ()),
                    datatypes=tuple(d.datatypes or ()),
                    identifier=d.identifier,
                    privileges=tuple(d.privileges or ()),
                )
                for d in rule.require.authorization_details
            )

        if not required:
            allowed = self._policy.defaults.unmatched == "allow"
            return PolicyDecision(allowed=allowed, carry=carry)

        missing = tuple(r for r in required if not covers(r, principal.authorization_details))
        return PolicyDecision(allowed=not missing, missing=missing, carry=carry)

    def dead_rule_warnings(self, operations: Sequence[Operation]) -> list[str]:
        warnings: list[str] = []
        for index, rule in enumerate(self._policy.rules):
            if not any(_matches(op, rule.match) for op in operations):
                warnings.append(
                    f"policy rule at index {index} (match={rule.match!r}) matched no "
                    "operations; it may be stale"
                )
        return warnings
