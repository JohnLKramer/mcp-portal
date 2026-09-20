"""Turn a stream of Operations into the final ToolSet.

Order is load-bearing: classify, then select, then name. Collision suffixes depend
on the surviving set, so naming before selection would let removing one operation
silently rename another.

Matching is on `id`, `tags`, `upstream`, and `effect` — never on `name`. Names are
unstable by construction, so a name-keyed rule that stops matching after a
collision suffix appears would fail open.
"""

import dataclasses
from collections.abc import Iterable, Sequence
from fnmatch import fnmatch
from typing import Any

from mcp_portal.config.models import ClassificationRule, Config, MatchSpec, SelectionConfig
from mcp_portal.naming import NamingOptions, generate_names
from mcp_portal.operations import Operation


@dataclasses.dataclass(frozen=True, slots=True)
class ToolSet:
    operations: tuple[Operation, ...]
    by_name: dict[str, Operation]
    warnings: tuple[str, ...] = ()


def _matches(op: Operation, spec: MatchSpec) -> bool:
    if spec.ids and not any(fnmatch(op.id, pattern) for pattern in spec.ids):
        return False
    if spec.tags and not set(spec.tags) & set(op.group_tags):
        return False
    if spec.upstream and op.upstream not in spec.upstream:
        return False
    return not (spec.effect and op.effect not in spec.effect)


def _classify(ops: Sequence[Operation], rules: Sequence[ClassificationRule]) -> list[Operation]:
    result: list[Operation] = []
    for op in ops:
        for rule in rules:  # later rules win
            if not _matches(op, rule.match):
                continue
            changes: dict[str, Any] = {}
            if rule.effect is not None:
                changes["effect"] = rule.effect
            if rule.sensitivity is not None:
                changes["sensitivity"] = rule.sensitivity
            if changes:
                op = dataclasses.replace(op, **changes)
        result.append(op)
    return result


def _select(
    ops: Sequence[Operation], selection: SelectionConfig
) -> tuple[list[Operation], list[str]]:
    warnings: list[str] = []
    used: set[str] = set()

    def keep(op: Operation) -> bool:
        if selection.include_tags is not None:
            if not set(selection.include_tags) & set(op.group_tags):
                return False
            used.update(set(selection.include_tags) & set(op.group_tags))
        if selection.include_ids is not None:
            hits = [p for p in selection.include_ids if fnmatch(op.id, p)]
            if not hits:
                return False
            used.update(hits)
        hit_tags = set(selection.exclude_tags) & set(op.group_tags)
        if hit_tags:
            used.update(hit_tags)
            return False
        hit_ids = [p for p in selection.exclude_ids if fnmatch(op.id, p)]
        if hit_ids:
            used.update(hit_ids)
            return False
        return True

    kept = [op for op in ops if keep(op)]

    declared = set(selection.exclude_tags) | set(selection.exclude_ids)
    declared |= set(selection.include_tags or [])
    declared |= set(selection.include_ids or [])
    for rule in sorted(declared - used):
        warnings.append(f"selection rule {rule!r} matched no operations; it may be stale")
    return kept, warnings


def build_toolset(operations: Iterable[Operation], config: Config) -> ToolSet:
    classified = _classify(list(operations), config.classification)
    selected, warnings = _select(classified, config.selection)

    options = NamingOptions(
        strategy=config.naming.strategy,
        prefix_with_group_tag=config.naming.prefix_with_group_tag,
        prefix_with_upstream=config.naming.prefix_with_upstream,
    )
    generated = generate_names(selected, options)

    named = tuple(dataclasses.replace(op, name=op.name or generated[op.id]) for op in selected)
    return ToolSet(
        operations=named,
        by_name={op.name: op for op in named},
        warnings=tuple(warnings),
    )
