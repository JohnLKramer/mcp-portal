"""Fetch and parse an OpenAPI document, and resolve its base URL.

The document is fetched once at startup (§6 of the design); there is no
runtime refresh. A tool surface that changes underneath a connected client is
a correctness problem, not a feature.
"""

import json
from pathlib import Path
from typing import Any

import httpx
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


class OpenApiError(Exception):
    """Raised for any problem loading, parsing, or resolving an OpenAPI document."""


def parse_document(
    text: str, *, is_yaml: bool, require_openapi_field: bool = True
) -> dict[str, Any]:
    """Parse JSON or YAML text.

    `require_openapi_field` is False for external `$ref` targets: a shared
    component-schema fragment has no top-level `openapi` version field, only
    the entry document does.
    """
    if is_yaml:
        try:
            data = YAML(typ="safe").load(text)
        except YAMLError as exc:
            raise OpenApiError(f"invalid YAML in OpenAPI document: {exc}") from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OpenApiError(f"invalid JSON in OpenAPI document: {exc}") from exc
    if not isinstance(data, dict):
        raise OpenApiError("not a mapping: expected a JSON/YAML object at the top level")
    if require_openapi_field and "openapi" not in data:
        raise OpenApiError("not an OpenAPI document: missing top-level 'openapi' version field")
    return data


def fetch_text(location: str, base_dir: Path, client: httpx.Client) -> tuple[str, bool]:
    """Return `(text, is_yaml)` for a URL or a file path relative to `base_dir`."""
    if location.startswith(("http://", "https://")):
        try:
            response = client.get(location, timeout=30.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OpenApiError(f"cannot fetch OpenAPI document {location!r}: {exc}") from exc
        content_type = response.headers.get("content-type", "")
        is_yaml = "yaml" in content_type or location.endswith((".yaml", ".yml"))
        return response.text, is_yaml

    path = Path(location)
    resolved = path if path.is_absolute() else base_dir / path
    try:
        text = resolved.read_text()
    except OSError as exc:
        raise OpenApiError(f"cannot read OpenAPI document {resolved}: {exc}") from exc
    return text, resolved.suffix in {".yaml", ".yml"}


def resolve_base_url(document: dict[str, Any], base_url_override: str | None) -> str:
    """`base_url` wins when set (§6); otherwise the document's first declared server."""
    if base_url_override:
        return base_url_override

    servers = document.get("servers") or []
    if not servers:
        raise OpenApiError(
            "no 'base_url' was configured and the document declares no servers[] to fall back to"
        )

    url: str = servers[0].get("url", "")
    for name, spec in (servers[0].get("variables") or {}).items():
        default = spec.get("default")
        if default is None:
            raise OpenApiError(f"server variable {name!r} has no default and cannot be resolved")
        url = url.replace("{" + name + "}", str(default))
    return url
