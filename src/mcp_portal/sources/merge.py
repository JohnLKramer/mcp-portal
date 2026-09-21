"""Merge introspected and explicit operations by id (§6 of the design).

An explicit entry whose `id` matches an introspected operation replaces it
entirely; an entry with a new `id` is added. This is how an operator corrects
a bad description or a wrong `effect` without abandoning introspection.
"""

from collections.abc import Iterable

from mcp_portal.operations import Operation


def merge_operations(
    introspected: Iterable[Operation], explicit: Iterable[Operation]
) -> list[Operation]:
    introspected_list = list(introspected)
    explicit_by_id = {op.id: op for op in explicit}

    merged = [explicit_by_id.get(op.id, op) for op in introspected_list]

    seen = {op.id for op in introspected_list}
    merged.extend(op for op in explicit_by_id.values() if op.id not in seen)
    return merged
