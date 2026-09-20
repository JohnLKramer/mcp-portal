"""Build and execute HTTP requests for an HttpBinding.

`build_request` is pure and is the highest-risk code in the system: it is the
boundary where model-supplied values become a real request.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from mcp_portal.operations import BodyMode, HttpBinding, ParamLocation

_CRLF = ("\r", "\n")


class RequestBuildError(Exception):
    """Raised when validated arguments still cannot form a safe request."""


@dataclass(frozen=True, slots=True)
class Credential:
    header: str
    value: str


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


def _encode_path_value(value: object) -> str:
    """Percent-encode against the RFC 3986 unreserved set.

    `safe=""` is the entire defence: it encodes `/`, `?`, `#` and `%`, so a value
    like `../admin/reset` cannot escape its path segment and reach an operation
    the registry never exposed.
    """
    text = str(value)
    if text == "":
        raise RequestBuildError(
            "empty value for a path parameter would collapse the segment and change the route"
        )
    return quote(text, safe="")


def _query_pairs(arg: str, value: object, explode: bool) -> list[tuple[str, str]]:
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
        if explode:
            return [(arg, v) for v in items]
        return [(arg, ",".join(items))]
    if isinstance(value, bool):
        return [(arg, "true" if value else "false")]
    return [(arg, str(value))]


def _check_header_value(name: str, value: str) -> str:
    if any(c in value for c in _CRLF):
        raise RequestBuildError(
            f"header {name!r} value contains CR or LF, which would allow header injection"
        )
    return value


def _join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def build_request(
    binding: HttpBinding,
    base_url: str,
    arguments: Mapping[str, Any],
    credential: Credential | None,
) -> PreparedRequest:
    path = binding.path
    query: list[tuple[str, str]] = []
    headers: dict[str, str] = {}
    body_fields: dict[str, Any] = {}

    for param in binding.parameters:
        if param.arg not in arguments:
            if param.required:
                raise RequestBuildError(f"missing required argument {param.arg!r}")
            continue
        value = arguments[param.arg]

        match param.location:
            case ParamLocation.PATH:
                path = path.replace("{" + param.wire_name + "}", _encode_path_value(value))
            case ParamLocation.QUERY:
                query.extend(_query_pairs(param.wire_name, value, param.explode))
            case ParamLocation.HEADER:
                headers[param.wire_name] = _check_header_value(param.wire_name, str(value))

    body: bytes | None = None
    if binding.body is not None:
        if binding.body.mode is BodyMode.SINGLE_ARG or binding.body.schema.get("type") != "object":
            if "body" not in arguments:
                raise RequestBuildError(f"missing required argument {'body'!r}")
            body = json.dumps(arguments["body"]).encode()
        else:
            declared = set(binding.body.schema.get("properties", {}))
            body_fields = {k: v for k, v in arguments.items() if k in declared}
            for name in binding.body.schema.get("required", []):
                if name not in arguments or name not in body_fields:
                    raise RequestBuildError(f"missing required argument {name!r}")
            body = json.dumps(body_fields).encode()
        if body is not None:
            headers["Content-Type"] = binding.body.content_type

    url = _join(base_url, path)
    if query:
        url = f"{url}?{urlencode(query)}"

    # Attached last so a model-supplied header can never displace the real credential.
    if credential is not None:
        headers[credential.header] = credential.value

    return PreparedRequest(method=binding.method, url=url, headers=headers, body=body)
