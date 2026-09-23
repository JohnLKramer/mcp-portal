"""Build Operations from a GraphQL upstream's introspected schema.

Entity tier only: one candidate Operation per top-level field on Query/
Mutation, mirroring how `sources/openapi.py` enumerates one candidate per
path+method. Field-tier filtering is `graphql_selection.build_selection_set`;
this module only decides which top-level fields exist and wires their
variables into an input schema, same as OpenAPI wires parameters.
"""

import logging
from collections.abc import Iterable, Mapping
from typing import Literal

from mcp_portal.classify import UnsupportedOperationType, effect_for_operation_type
from mcp_portal.operations import GraphQlBinding, Operation, Sensitivity, Variable
from mcp_portal.sources.graphql_introspection import (
    FieldInfo,
    IntrospectedSchema,
    named_type,
    render_type_ref,
)
from mcp_portal.sources.graphql_selection import SelectionError, build_selection_set

log = logging.getLogger("mcp_portal")


def _document(
    operation_type: Literal["query", "mutation"], field: FieldInfo, selection: str | None
) -> str:
    args = ", ".join(f"${arg.name}: {render_type_ref(arg.type)}" for arg in field.args)
    call_args = ", ".join(f"{arg.name}: ${arg.name}" for arg in field.args)
    var_decl = f"({args})" if args else ""
    call = f"{field.name}({call_args})" if call_args else field.name
    call_with_selection = f"{call} {selection}" if selection is not None else call
    return f"{operation_type}{var_decl} {{ {call_with_selection} }}"


def _input_schema(field: FieldInfo) -> dict[str, object]:
    properties = {arg.name: {"type": "string"} for arg in field.args}
    required = [arg.name for arg in field.args]
    return {"type": "object", "properties": properties, "required": required}


class GraphQlSource:
    def __init__(
        self, upstream_key: str, schema: IntrospectedSchema, type_policy: Mapping[str, list[str]]
    ) -> None:
        self._upstream_key = upstream_key
        self._schema = schema
        self._type_policy = type_policy

    def operations(self) -> Iterable[Operation]:
        roots: tuple[tuple[Literal["query", "mutation"], str | None], ...] = (
            ("query", self._schema.query_type),
            ("mutation", self._schema.mutation_type),
        )
        for operation_type, type_name in roots:
            if type_name is None:
                continue
            root = self._schema.types_by_name.get(type_name)
            if root is None:
                continue
            for field in root.fields:
                op = self._build(operation_type, field)
                if op is not None:
                    yield op

    def _build(
        self, operation_type: Literal["query", "mutation"], field: FieldInfo
    ) -> Operation | None:
        try:
            effect = effect_for_operation_type(operation_type)
        except UnsupportedOperationType:
            return None

        return_type = named_type(field.type)
        selection: str | None
        if return_type.kind not in ("OBJECT", "INTERFACE"):
            # Scalars, enums, and unions carry no sub-selection at all — not
            # even an empty `{ }` or `{ __typename }`.
            selection = None
        elif return_type.name is None:
            selection = "{ __typename }"
        else:
            try:
                selection = build_selection_set(return_type.name, self._schema, self._type_policy)
            except SelectionError as exc:
                log.warning(
                    "upstream %r: dropping %s %s: %s",
                    self._upstream_key,
                    operation_type,
                    field.name,
                    exc,
                )
                return None

        binding = GraphQlBinding(
            operation_type=operation_type,
            document=_document(operation_type, field, selection),
            variables=tuple(
                Variable(
                    name=a.name,
                    graphql_type=render_type_ref(a.type),
                    required=a.type.kind == "NON_NULL",
                )
                for a in field.args
            ),
        )

        return Operation(
            id=field.name,
            upstream=self._upstream_key,
            name="",
            title=field.name,
            description=field.name,
            group_tags=(),
            effect=effect,
            sensitivity=Sensitivity.NORMAL,
            input_schema=_input_schema(field),
            binding=binding,
        )
