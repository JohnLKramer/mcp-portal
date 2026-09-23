"""Generate a default GraphQL selection set from introspection.

Only exercised for introspection-generated operations — a hand-authored
`operations[]` entry's document already is the selection, and this module
never runs against it. Two-tier field policy (§ design spec "Two-tier
selection"): the entity tier (which top-level fields become operations) is
handled by `sources/graphql.py`; this module is the field tier — which
fields of a returned type are included once an operation is already chosen.
"""

from collections.abc import Mapping

from mcp_portal.sources.graphql_introspection import IntrospectedSchema, TypeInfo, named_type


class SelectionError(Exception):
    """Raised when a return type has no corresponding entry in the schema."""


def _fields_for(
    type_name: str,
    schema: IntrospectedSchema,
    type_policy: Mapping[str, list[str]],
    path: tuple[str, ...],
    indent: str,
) -> list[str]:
    type_info: TypeInfo | None = schema.types_by_name.get(type_name)
    if type_info is None:
        raise SelectionError(f"type {type_name!r} is not defined in the introspected schema")

    excluded = set(type_policy.get(type_name, []))
    lines: list[str] = []
    for field in type_info.fields:
        if field.name in excluded:
            continue
        if field.args:
            # A field the generator cannot supply arguments for is dropped
            # rather than included with no way to satisfy its requirements —
            # the same "skip and warn" posture OpenAPI takes for anything it
            # cannot flatten. Hand-authoring is the escape hatch.
            continue

        target = named_type(field.type)
        if target.kind not in ("OBJECT", "INTERFACE"):
            lines.append(f"{indent}{field.name}")
            continue

        assert target.name is not None
        if target.name in path:
            lines.append(f"{indent}{field.name} {{ __typename }}")
            continue  # stop at first re-occurrence, per the design's cycle rule

        nested = _fields_for(target.name, schema, type_policy, (*path, target.name), indent + "  ")
        if nested:
            lines.append(f"{indent}{field.name} {{")
            lines.extend(nested)
            lines.append(f"{indent}}}")
    return lines


def build_selection_set(
    type_name: str, schema: IntrospectedSchema, type_policy: Mapping[str, list[str]]
) -> str:
    """Return the `{ ... }` selection-set text for `type_name`, field-tier-filtered."""
    lines = _fields_for(type_name, schema, type_policy, (type_name,), "  ")
    return "{\n" + "\n".join(lines) + "\n}"
