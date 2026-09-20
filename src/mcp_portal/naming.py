"""Generate MCP tool names from operations.

Names are presentation only. They change with strategy, prefixes, truncation and
collision suffixes, so nothing in the system matches on them — `Operation.id` is
the match key everywhere.

Runs after selection: collision suffixes depend on the surviving set, so naming
first would let removing one operation silently rename another.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from mcp_portal.operations import HttpBinding, Operation

# Several MCP clients cap tool names at 64 characters. Upstream and tag prefixes
# plus a collision suffix consume that budget quickly. Fixed, not configurable.
MAX_NAME_LENGTH = 64
_SUFFIX_LENGTH = 6
_TRUNCATE_TO = MAX_NAME_LENGTH - _SUFFIX_LENGTH - 1

_INVALID = re.compile(r"[^a-z0-9]+")
NAME_PATTERN = re.compile(rf"[a-z0-9_]{{1,{MAX_NAME_LENGTH}}}")


class NameCollisionError(Exception):
    """Raised when a tool-name collision survives hash suffixing."""


@dataclass(frozen=True, slots=True)
class NamingOptions:
    strategy: str = "operation_id"
    prefix_with_group_tag: bool = False
    prefix_with_upstream: bool = False


def normalize(text: str) -> str:
    """Lowercase and reduce to `[a-z0-9_]`, collapsing runs of separators."""
    return _INVALID.sub("_", text.lower()).strip("_")


def _base_name(op: Operation, options: NamingOptions) -> str:
    if options.strategy == "method_path" and isinstance(op.binding, HttpBinding):
        core = f"{op.binding.method}_{op.binding.path}"
    else:
        core = op.id

    parts: list[str] = []
    if options.prefix_with_upstream:
        parts.append(op.upstream)
    if options.prefix_with_group_tag and op.group_tags:
        parts.append(op.group_tags[0])
    parts.append(core)
    return normalize("_".join(parts))


def _suffix(op_id: str) -> str:
    return hashlib.sha256(op_id.encode()).hexdigest()[:_SUFFIX_LENGTH]


def _cap(name: str) -> str:
    if len(name) <= MAX_NAME_LENGTH:
        return name
    return f"{name[:_TRUNCATE_TO]}_{_suffix(name)}"


def generate_names(
    operations: Iterable[Operation],
    options: NamingOptions,
) -> dict[str, str]:
    """Map operation id to tool name, resolving collisions deterministically."""
    ops = list(operations)

    ids = [op.id for op in ops]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise NameCollisionError(f"duplicate operation ids: {sorted(duplicates)}")

    bases = {op.id: _cap(_base_name(op, options)) for op in ops}

    counts: dict[str, int] = {}
    for base in bases.values():
        counts[base] = counts.get(base, 0) + 1

    names: dict[str, str] = {}
    for op_id in sorted(bases):
        base = bases[op_id]
        if counts[base] == 1:
            names[op_id] = base
            continue
        trimmed = base[:_TRUNCATE_TO]
        names[op_id] = f"{trimmed}_{_suffix(op_id)}"

    final = list(names.values())
    unresolved = {n for n in final if final.count(n) > 1}
    if unresolved:
        raise NameCollisionError(f"tool name collision survived suffixing: {sorted(unresolved)}")

    return names
