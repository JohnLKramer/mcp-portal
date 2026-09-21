"""Build Operations from an OpenAPI document.

Mirrors `sources/explicit.py`: the same flatten and classify pipeline produces
identical tool schemas for identical bindings, whether the binding came from
config or from introspection. The difference is failure mode — an explicit
entry that fails to flatten is an operator's mistake and a hard config error;
an introspected operation that fails to flatten is a third party's document
and is skipped with a warning instead (§6 of the design).
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from mcp_portal.classify import UnsupportedMethod, effect_for_method
from mcp_portal.config.models import OpenApiIntrospectionConfig
from mcp_portal.naming import NAME_PATTERN
from mcp_portal.operations import (
    BodySpec,
    HttpBinding,
    Operation,
    Parameter,
    ParamLocation,
    Sensitivity,
)
from mcp_portal.sources.dialect import convert_30_schema, is_openapi_31
from mcp_portal.sources.flatten import (
    FlattenError,
    build_input_schema,
    check_path_template,
    resolve_arg_names,
)
from mcp_portal.sources.openapi_document import (
    fetch_text,
    parse_document,
    resolve_base_url,
)
from mcp_portal.sources.openapi_paths import (
    RawOperation,
    default_operation_id,
    extract_raw_operations,
)
from mcp_portal.sources.refs import RefResolver

log = logging.getLogger("mcp_portal")

_LOCATION = {
    "path": ParamLocation.PATH,
    "query": ParamLocation.QUERY,
    "header": ParamLocation.HEADER,
}


@dataclass(frozen=True, slots=True)
class LoadedDocument:
    document: dict[str, Any]
    base_url: str
    warnings: tuple[str, ...]


def _fetch_external(target: str, base_dir: Path, client: httpx.Client) -> dict[str, Any]:
    text, is_yaml = fetch_text(target, base_dir, client)
    # An external $ref target is typically a component-schema fragment, not a
    # full OpenAPI document — it has no top-level `openapi` version field.
    return parse_document(text, is_yaml=is_yaml, require_openapi_field=False)


def load_document(
    config: OpenApiIntrospectionConfig,
    base_dir: Path,
    base_url_override: str | None,
    client: httpx.Client,
) -> LoadedDocument:
    location = config.url or config.file
    assert location is not None  # enforced by OpenApiIntrospectionConfig's validator
    text, is_yaml = fetch_text(location, base_dir, client)
    raw = parse_document(text, is_yaml=is_yaml)

    resolver = RefResolver(
        raw,
        allow_external=config.allow_external_refs,
        allowed_hosts=frozenset(config.allowed_hosts),
        fetch_external=lambda target: _fetch_external(target, base_dir, client),
    )
    resolved = resolver.resolve()

    if not is_openapi_31(str(resolved.get("openapi", ""))):
        resolved = convert_30_schema(resolved)

    base_url = resolve_base_url(resolved, base_url_override)
    return LoadedDocument(document=resolved, base_url=base_url, warnings=tuple(resolver.warnings))


def _binding(raw: RawOperation) -> HttpBinding:
    parameters = tuple(
        Parameter(
            arg=p.name,
            location=_LOCATION[p.location],
            wire_name=p.name,
            required=p.required,
            schema=p.schema,
            explode=p.explode,
        )
        for p in raw.parameters
    )
    body = (
        BodySpec(content_type=raw.request_body["content_type"], schema=raw.request_body["schema"])
        if raw.request_body is not None
        else None
    )
    return HttpBinding(method=raw.method.upper(), path=raw.path, parameters=parameters, body=body)


class OpenApiSource:
    def __init__(
        self, upstream_key: str, loaded: LoadedDocument, *, include_deprecated: bool
    ) -> None:
        self._upstream_key = upstream_key
        self._document = loaded.document
        self._include_deprecated = include_deprecated

    def operations(self) -> Iterable[Operation]:
        for raw in extract_raw_operations(
            self._document, include_deprecated=self._include_deprecated
        ):
            op = self._build(raw)
            if op is not None:
                yield op

    def _build(self, raw: RawOperation) -> Operation | None:
        if raw.extensions.get("x-mcp-exclude"):
            return None

        binding = _binding(raw)
        try:
            derived_effect = effect_for_method(binding.method)
        except UnsupportedMethod:
            return None

        try:
            check_path_template(binding)
            resolved = resolve_arg_names(binding)
            input_schema = build_input_schema(binding)
        except FlattenError as exc:
            log.warning(
                "upstream %r: dropping %s %s: %s",
                self._upstream_key,
                raw.method.upper(),
                raw.path,
                exc,
            )
            return None

        op_id = raw.operation_id or _default_id(raw)
        # str(...) coerces whatever wins: an x-mcp-* extension is a raw document
        # value of unknown type, and a non-string here must not crash the load —
        # the same "one bad operation must not fail the whole load" principle
        # that governs the FlattenError handling above.
        description = str(
            raw.extensions.get("x-mcp-description") or raw.description or raw.summary or op_id
        )
        title = str(
            raw.extensions.get("x-mcp-title")
            or raw.summary
            or next(iter(description.splitlines()), op_id)
        )
        name = self._name_override(raw)

        return Operation(
            id=op_id,
            upstream=self._upstream_key,
            name=name,
            title=title,
            description=description,
            group_tags=raw.tags,
            effect=derived_effect,
            sensitivity=Sensitivity.SENSITIVE
            if raw.extensions.get("x-mcp-sensitive")
            else Sensitivity.NORMAL,
            input_schema=input_schema,
            binding=resolved,
        )

    def _name_override(self, raw: RawOperation) -> str:
        name = str(raw.extensions.get("x-mcp-name") or "")
        if not name:
            return ""
        if not NAME_PATTERN.fullmatch(name):
            log.warning(
                "upstream %r: x-mcp-name %r on %s %s does not match the published tool-name "
                "pattern; falling back to a generated name",
                self._upstream_key,
                name,
                raw.method.upper(),
                raw.path,
            )
            return ""
        return name


def _default_id(raw: RawOperation) -> str:
    return default_operation_id(raw.method, raw.path)
