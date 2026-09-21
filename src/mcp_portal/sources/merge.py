"""Merge introspected and explicit operations by id (§6 of the design).

An explicit entry whose `id` matches an introspected operation replaces it
entirely; an entry with a new `id` is added. This is how an operator corrects
a bad description or a wrong `effect` without abandoning introspection.
"""

from collections.abc import Iterable

from mcp_portal.naming import NameCollisionError
from mcp_portal.operations import Operation


def merge_operations(
    introspected: Iterable[Operation], explicit: Iterable[Operation]
) -> list[Operation]:
    introspected_list = list(introspected)
    explicit_list = list(explicit)

    # Building `explicit_by_id` below silently keeps only the last of any
    # duplicate id, which is exactly the "duplicate operation ids" load error
    # P1's registry.py already raises for the explicit-only case — checking
    # here, before that dict exists, is what keeps this a load error instead
    # of a silent last-write-wins.
    ids = [op.id for op in explicit_list]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise NameCollisionError(f"duplicate operation ids in operations[]: {duplicates}")

    explicit_by_id = {op.id: op for op in explicit_list}
    merged = [explicit_by_id.get(op.id, op) for op in introspected_list]

    seen = {op.id for op in introspected_list}
    merged.extend(op for op in explicit_by_id.values() if op.id not in seen)
    return merged
