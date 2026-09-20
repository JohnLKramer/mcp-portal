"""Synthesize an MCP input schema from a binding.

A flat object schema is materially easier for a model to fill in correctly than a
nested {path, query, body} shape, and correctness at the tool boundary is the point
of the product. Flattening is presentation only: `Parameter.wire_name` retains the
name the upstream expects.
"""

import re
from typing import Any

from mcp_portal.operations import BodyMode, BodySpec, HttpBinding, Parameter, ParamLocation

SUPPORTED_CONTENT_TYPES = frozenset({"application/json"})

BODY_ARG = "body"

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")

# Composition and constraint keywords a flattened body schema would declare and
# the flattened tool schema would not carry. Lifting `properties` and `required`
# out of a body is lossless only when the body declares nothing else.
DROPPED_BODY_KEYWORDS = (
    "oneOf",
    "anyOf",
    "allOf",
    "not",
    "if",
    "then",
    "else",
    "additionalProperties",
    "unevaluatedProperties",
    "patternProperties",
    "propertyNames",
    "dependentRequired",
    "dependentSchemas",
    "minProperties",
    "maxProperties",
)


class FlattenError(Exception):
    """Raised at load time when a binding cannot produce a usable tool schema."""


def _body_properties(body: BodySpec) -> dict[str, Any] | None:
    """Top-level properties to lift, or None when the body stays a single argument."""
    if body.mode is BodyMode.SINGLE_ARG:
        return None
    if body.schema.get("type") != "object":
        return None
    return dict(body.schema.get("properties", {}))


def _check_content_type(body: BodySpec | None) -> None:
    if body is not None and body.content_type not in SUPPORTED_CONTENT_TYPES:
        raise FlattenError(
            f"unsupported request body content type {body.content_type!r}; "
            f"P1 supports {sorted(SUPPORTED_CONTENT_TYPES)}"
        )


def check_path_template(binding: HttpBinding) -> None:
    """Reconcile `{...}` placeholders in the path with declared path parameters.

    All three mismatches are invisible at call time, which is why they are errors
    at load time: a placeholder with no parameter is never substituted and ships a
    literal `{id}` to the upstream, a path parameter with no placeholder has its
    value read and then dropped, two parameters on one placeholder leave whichever
    substitutes second with nowhere to go, and an optional path parameter produces
    one of these depending on what the caller happened to pass.
    """
    placeholders = _PLACEHOLDER.findall(binding.path)
    repeated = sorted({p for p in placeholders if placeholders.count(p) > 1})
    if repeated:
        raise FlattenError(f"path {binding.path!r} repeats placeholder(s) {repeated}")

    declared: dict[str, str] = {}
    for p in binding.parameters:
        if p.location is not ParamLocation.PATH:
            continue
        if not p.required:
            raise FlattenError(
                f"path parameter {p.arg!r} is optional, but a path parameter cannot be: "
                f"omitting it would leave the literal placeholder '{{{p.wire_name}}}' in the URL"
            )
        if p.wire_name in declared:
            raise FlattenError(
                f"path parameters {declared[p.wire_name]!r} and {p.arg!r} share wire name "
                f"{p.wire_name!r}, so only one of them can fill the placeholder "
                f"'{{{p.wire_name}}}' and the other's value would be discarded"
            )
        declared[p.wire_name] = p.arg

    unmatched = sorted(set(placeholders) - set(declared))
    if unmatched:
        raise FlattenError(
            f"path {binding.path!r} has placeholder(s) {unmatched} with no path parameter to fill "
            "them"
        )

    unplaced = sorted(declared[w] for w in set(declared) - set(placeholders))
    if unplaced:
        raise FlattenError(
            f"path parameter(s) {unplaced} have no matching placeholder in path {binding.path!r}, "
            "so their values would be read and discarded"
        )


def unsupported_body_keywords(body: BodySpec | None) -> list[str]:
    """Keywords a flattened body schema declares that the tool schema will not carry."""
    if body is None or _body_properties(body) is None:
        return []
    return [k for k in DROPPED_BODY_KEYWORDS if k in body.schema]


def resolve_arg_names(binding: HttpBinding) -> HttpBinding:
    """Return the binding with colliding argument names prefixed by location.

    A name that still collides after prefixing is an error rather than a second
    rename, because a twice-renamed argument is one no operator can predict.
    """
    _check_content_type(binding.body)

    counts: dict[str, int] = {}
    for p in binding.parameters:
        counts[p.arg] = counts.get(p.arg, 0) + 1

    body_props = _body_properties(binding.body) if binding.body else None
    body_names = (
        set(body_props) if body_props is not None else ({BODY_ARG} if binding.body else set())
    )
    for name in body_names:
        counts[name] = counts.get(name, 0) + 1

    renamed: list[Parameter] = []
    for p in binding.parameters:
        arg = f"{p.location.value}_{p.arg}" if counts[p.arg] > 1 else p.arg
        renamed.append(
            Parameter(
                arg=arg,
                location=p.location,
                wire_name=p.wire_name,
                required=p.required,
                schema=p.schema,
                style=p.style,
                explode=p.explode,
            )
        )

    seen: dict[str, str] = {}
    for p in renamed:
        if p.arg in seen:
            raise FlattenError(
                f"argument name {p.arg!r} collides after location prefixing: "
                f"contributed by {seen[p.arg]} and {p.location.value}"
            )
        seen[p.arg] = p.location.value
    for name in sorted(body_names):
        if name in seen:
            raise FlattenError(
                f"argument name {name!r} collides after location prefixing: "
                f"contributed by {seen[name]} and body"
            )
        seen[name] = "body"

    return HttpBinding(
        method=binding.method,
        path=binding.path,
        parameters=tuple(renamed),
        body=binding.body,
        protocol=binding.protocol,
    )


def build_input_schema(binding: HttpBinding) -> dict[str, Any]:
    """Build a self-contained JSON Schema 2020-12 object schema for the tool."""
    resolved = resolve_arg_names(binding)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for p in resolved.parameters:
        properties[p.arg] = dict(p.schema)
        if p.required:
            required.append(p.arg)

    if resolved.body is not None:
        body_props = _body_properties(resolved.body)
        if body_props is None:
            properties[BODY_ARG] = dict(resolved.body.schema)
            required.append(BODY_ARG)
        else:
            properties.update(body_props)
            required.extend(resolved.body.schema.get("required", []))

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
