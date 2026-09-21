"""Convert an OpenAPI 3.0 schema object to JSON Schema 2020-12.

OpenAPI 3.1 schemas *are* JSON Schema 2020-12 and pass through unchanged.
OpenAPI 3.0 uses a near-miss dialect — `nullable: true`, boolean
`exclusiveMinimum`/`exclusiveMaximum`, singular `example` — that produces a
subtly wrong tool schema if passed through untouched, in ways that surface
only at call time.
"""

from typing import Any


def is_openapi_31(version: str) -> bool:
    return version.startswith("3.1")


def convert_30_schema(schema: Any) -> Any:
    """Recursively convert one OpenAPI 3.0 schema object. Non-schema values pass through."""
    if isinstance(schema, list):
        return [convert_30_schema(v) for v in schema]
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("nullable", "exclusiveMinimum", "exclusiveMaximum") and isinstance(value, bool):
            continue
        if key == "example":
            out["examples"] = [convert_30_schema(value)]
            continue
        out[key] = convert_30_schema(value)

    if schema.get("nullable") is True:
        if "type" in out:
            base = out["type"]
            types = base if isinstance(base, list) else [base]
            out["type"] = sorted({*types, "null"})
        else:
            # No sibling `type` to widen — most commonly a nullable `$ref`,
            # the standard OpenAPI 3.0 workaround for nullable references.
            # Wrapping in `anyOf` is the only way to add null without a type.
            out = {"anyOf": [out, {"type": "null"}]}

    if schema.get("exclusiveMinimum") is True and "minimum" in out:
        out["exclusiveMinimum"] = out.pop("minimum")
    if schema.get("exclusiveMaximum") is True and "maximum" in out:
        out["exclusiveMaximum"] = out.pop("maximum")

    return out
