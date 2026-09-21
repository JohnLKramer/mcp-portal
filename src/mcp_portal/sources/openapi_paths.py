"""Walk an OpenAPI document's `paths` object into per-operation records.

Kept separate from binding construction (Task 6) so the parameter-merge and
filtering rules unique to OpenAPI are testable without touching
`operations.py`'s types at all.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mcp_portal.classify import EXPOSED_METHODS

_SLUG_INVALID = re.compile(r"[^a-z0-9]+")
_PARAM_LOCATIONS = frozenset({"path", "query", "header"})


def default_operation_id(method: str, path: str) -> str:
    """Stable identity for an operation with no `operationId` (§4 of the design)."""
    return _SLUG_INVALID.sub("_", f"{method}_{path}".lower()).strip("_")


@dataclass(frozen=True, slots=True)
class RawParameter:
    name: str
    location: str
    required: bool
    schema: Mapping[str, Any]
    explode: bool


@dataclass(frozen=True, slots=True)
class RawOperation:
    method: str
    path: str
    operation_id: str | None
    summary: str | None
    description: str | None
    tags: tuple[str, ...]
    parameters: tuple[RawParameter, ...]
    request_body: dict[str, Any] | None
    extensions: Mapping[str, Any]


def _raw_parameter(param: Mapping[str, Any]) -> RawParameter | None:
    location = param.get("in")
    if location not in _PARAM_LOCATIONS:
        return None  # "cookie" has no ParamLocation counterpart; never exposed.
    return RawParameter(
        name=param["name"],
        location=location,
        required=bool(param.get("required", False)),
        schema=param.get("schema", {"type": "string"}),
        explode=param.get("explode", True),
    )


def _merged_parameters(
    path_params: list[dict[str, Any]], op_params: list[dict[str, Any]]
) -> tuple[RawParameter, ...]:
    """Merge path-level and operation-level parameters; operation-level wins on
    a `(name, in)` collision, per OpenAPI's own precedence rule."""
    by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
    for param in path_params:
        by_key[(param.get("name"), param.get("in"))] = param
    for param in op_params:
        by_key[(param.get("name"), param.get("in"))] = param

    return tuple(p for raw in by_key.values() if (p := _raw_parameter(raw)) is not None)


def _request_body(op: Mapping[str, Any]) -> dict[str, Any] | None:
    body = op.get("requestBody")
    if not body:
        return None
    content = body.get("content", {})
    if not content:
        return None
    content_type = "application/json" if "application/json" in content else next(iter(content))
    return {"content_type": content_type, "schema": content[content_type].get("schema", {})}


def _extensions(op: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in op.items() if k.startswith("x-mcp-")}


def extract_raw_operations(
    document: Mapping[str, Any], *, include_deprecated: bool = False
) -> list[RawOperation]:
    operations: list[RawOperation] = []
    paths = document.get("paths", {})
    for path in sorted(paths):
        path_item = paths[path]
        path_params = path_item.get("parameters", [])
        for method in sorted(path_item):
            if method.upper() not in EXPOSED_METHODS:
                continue
            op = path_item[method]
            if op.get("deprecated") and not include_deprecated:
                continue
            operations.append(
                RawOperation(
                    method=method,
                    path=path,
                    operation_id=op.get("operationId"),
                    summary=op.get("summary"),
                    description=op.get("description"),
                    tags=tuple(op.get("tags", [])),
                    parameters=_merged_parameters(path_params, op.get("parameters", [])),
                    request_body=_request_body(op),
                    extensions=_extensions(op),
                )
            )
    return operations
