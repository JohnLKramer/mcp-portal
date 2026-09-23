"""Fetch and parse a GraphQL upstream's schema via standard introspection.

Unlike OpenAPI, introspection returns the schema as plain JSON with no `$ref`
resolution or YAML/JSON dialect to handle — one POST, one parse.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

# The canonical introspection query (graphql-js's getIntrospectionQuery),
# trimmed to what this project uses: no directives, no enumValues,
# interfaces/possibleTypes only as far as needed to recognize the kind.
_INTROSPECTION_QUERY = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    types {
      kind
      name
      fields(includeDeprecated: false) {
        name
        args { name type { ...TypeRef } }
        type { ...TypeRef }
      }
    }
  }
}
fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType {
          kind
          name
          ofType {
            kind
            name
            ofType { kind name }
          }
        }
      }
    }
  }
}
"""


class GraphQlIntrospectionError(Exception):
    """Raised for any problem fetching or parsing a GraphQL schema."""


@dataclass(frozen=True, slots=True)
class TypeRef:
    kind: str
    name: str | None
    of_type: TypeRef | None


@dataclass(frozen=True, slots=True)
class FieldArg:
    name: str
    type: TypeRef


@dataclass(frozen=True, slots=True)
class FieldInfo:
    name: str
    type: TypeRef
    args: tuple[FieldArg, ...] = ()


@dataclass(frozen=True, slots=True)
class TypeInfo:
    kind: str
    name: str
    fields: tuple[FieldInfo, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class IntrospectedSchema:
    query_type: str
    mutation_type: str | None
    types_by_name: Mapping[str, TypeInfo]


def _parse_type_ref(raw: Mapping[str, Any] | None) -> TypeRef:
    if raw is None:
        raise GraphQlIntrospectionError("expected a type reference, found null")
    return TypeRef(kind=raw["kind"], name=raw.get("name"), of_type=_opt_type_ref(raw.get("ofType")))


def _opt_type_ref(raw: Mapping[str, Any] | None) -> TypeRef | None:
    return None if raw is None else _parse_type_ref(raw)


def render_type_ref(ref: TypeRef) -> str:
    """Render a `TypeRef` back to SDL text, e.g. `ID!`, `[String]`, `[Int!]!`."""
    if ref.kind == "NON_NULL":
        assert ref.of_type is not None
        return f"{render_type_ref(ref.of_type)}!"
    if ref.kind == "LIST":
        assert ref.of_type is not None
        return f"[{render_type_ref(ref.of_type)}]"
    assert ref.name is not None
    return ref.name


def named_type(ref: TypeRef) -> TypeRef:
    """Unwrap NON_NULL/LIST wrappers down to the innermost named type."""
    while ref.kind in ("NON_NULL", "LIST"):
        assert ref.of_type is not None
        ref = ref.of_type
    return ref


def parse_introspection_result(data: Mapping[str, Any]) -> IntrospectedSchema:
    schema = data.get("data", {}).get("__schema") if isinstance(data.get("data"), dict) else None
    if schema is None:
        raise GraphQlIntrospectionError("introspection response has no data.__schema")

    types_by_name: dict[str, TypeInfo] = {}
    for raw_type in schema.get("types", []):
        name = raw_type.get("name")
        if name is None or name.startswith("__"):
            continue  # meta-types (__Type, __Field, ...) are never operations or return types
        fields = tuple(
            FieldInfo(
                name=raw_field["name"],
                type=_parse_type_ref(raw_field["type"]),
                args=tuple(
                    FieldArg(name=a["name"], type=_parse_type_ref(a["type"]))
                    for a in raw_field.get("args", [])
                ),
            )
            for raw_field in (raw_type.get("fields") or [])
        )
        types_by_name[name] = TypeInfo(kind=raw_type["kind"], name=name, fields=fields)

    query_type = schema.get("queryType", {}).get("name")
    if query_type is None:
        raise GraphQlIntrospectionError("introspection response has no queryType")
    mutation_type = (schema.get("mutationType") or {}).get("name")

    return IntrospectedSchema(
        query_type=query_type, mutation_type=mutation_type, types_by_name=types_by_name
    )


def fetch_schema(url: str, client: httpx.Client) -> IntrospectedSchema:
    response = client.post(url, json={"query": _INTROSPECTION_QUERY})
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise GraphQlIntrospectionError(
            f"introspection query failed: {body['errors'][0].get('message', body['errors'])}"
        )
    return parse_introspection_result(body)
