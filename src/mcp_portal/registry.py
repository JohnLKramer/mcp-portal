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
from mcp_portal.naming import NameCollisionError, NamingOptions, generate_names
from mcp_portal.operations import Effect, Operation, Sensitivity


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


def apply_mode_posture(operations: Sequence[Operation], mode: str) -> list[Operation]:
    """`introspect-safe` keeps only read-only, non-sensitive operations (§2, §4).

    Every other mode is unfiltered here: `configured` never introspects, and
    `introspect-unsafe` is unfiltered by definition — its gate is the
    `acknowledge_unsafe` config field, checked once at load (Task 1), not a
    per-operation filter here.
    """
    if mode != "introspect-safe":
        return list(operations)
    return [
        op
        for op in operations
        if op.effect is Effect.READ_ONLY and op.sensitivity is Sensitivity.NORMAL
    ]


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


def _assign_names(selected: Sequence[Operation], options: NamingOptions) -> tuple[Operation, ...]:
    """Give every surviving operation a unique tool name.

    An explicit `operations[].name` skips generation but not collision checking.
    Publishing two tools under one name means `by_name` routes a call to whichever
    operation happened to be last, while the annotations advertised for that name
    came from the other one — so a collision is a load-time error, not a silent win.
    """
    ids = [op.id for op in selected]
    duplicate_ids = sorted({i for i in ids if ids.count(i) > 1})
    if duplicate_ids:
        raise NameCollisionError(f"duplicate operation ids: {duplicate_ids}")

    # Explicit names are reserved first, so a generated name never displaces one.
    owner: dict[str, str] = {}
    for op in sorted((o for o in selected if o.name), key=lambda o: o.id):
        if op.name in owner:
            raise NameCollisionError(
                f"operations {owner[op.name]!r} and {op.id!r} both declare tool name {op.name!r}"
            )
        owner[op.name] = op.id

    generated = generate_names([op for op in selected if not op.name], options)
    for op_id in sorted(generated):
        name = generated[op_id]
        if name in owner:
            raise NameCollisionError(
                f"operation {op_id!r} generates tool name {name!r}, which operation "
                f"{owner[name]!r} declares explicitly"
            )
        owner[name] = op_id

    return tuple(dataclasses.replace(op, name=op.name or generated[op.id]) for op in selected)


def build_toolset(operations: Iterable[Operation], config: Config) -> ToolSet:
    classified = _classify(list(operations), config.classification)
    postured = apply_mode_posture(classified, config.mode)
    selected, warnings = _select(postured, config.selection)

    options = NamingOptions(
        strategy=config.naming.strategy,
        prefix_with_group_tag=config.naming.prefix_with_group_tag,
        prefix_with_upstream=config.naming.prefix_with_upstream,
    )
    named = _assign_names(selected, options)

    by_name = {op.name: op for op in named}
    assert len(by_name) == len(named), "a tool name was shadowed despite the collision check"
    return ToolSet(operations=named, by_name=by_name, warnings=tuple(warnings))
