"""Fetch and parse every upstream's OpenAPI document, when configured.

Shared by `app.build_app` (which also wires transports and auth) and
`configure` (which only needs the operations and resolved base URLs, never
a runnable app).
"""

import logging
import re
from pathlib import Path

import httpx

from mcp_portal.config.loader import ConfigError, credential_headers_for
from mcp_portal.config.models import HTTP_URL_PATTERN, McpPortalConfig
from mcp_portal.operations import Operation
from mcp_portal.sources.graphql import GraphQlSource
from mcp_portal.sources.graphql_introspection import GraphQlIntrospectionError, fetch_schema
from mcp_portal.sources.openapi import OpenApiSource, load_document
from mcp_portal.sources.openapi_document import OpenApiError
from mcp_portal.sources.refs import RefError

log = logging.getLogger("mcp_portal")


def introspect_upstreams(
    config: McpPortalConfig, base_dir: Path
) -> tuple[list[Operation], dict[str, str]]:
    """Returns the introspected operations and each upstream's resolved base
    URL — `base_url` when the operator set one, otherwise the document's own
    `servers[]`."""
    introspected: list[Operation] = []
    resolved_base_urls: dict[str, str] = {}
    with httpx.Client() as client:
        for key, upstream in config.upstreams.items():
            resolved_base_urls[key] = upstream.base_url or ""
            if upstream.introspection is None:
                continue
            try:
                if upstream.introspection.openapi is not None:
                    loaded = load_document(
                        upstream.introspection.openapi, base_dir, upstream.base_url, client
                    )
                    for warning in loaded.warnings:
                        log.warning("upstream %r: %s", key, warning)

                    resolved = upstream.base_url or loaded.base_url
                    if not re.match(HTTP_URL_PATTERN, resolved):
                        raise ConfigError(
                            f"upstream {key!r}: document server URL {resolved!r} is not an "
                            "absolute http(s) URL"
                        )
                    resolved_base_urls[key] = resolved

                    source = OpenApiSource(
                        key,
                        loaded,
                        include_deprecated=upstream.introspection.openapi.include_deprecated,
                        credential_headers=credential_headers_for(config),
                    )
                    introspected.extend(source.operations())
                else:
                    assert upstream.introspection.graphql is not None
                    schema = fetch_schema(upstream.introspection.graphql.url, client)
                    introspected.extend(
                        GraphQlSource(
                            key, schema, type_policy=upstream.introspection.graphql.type_policy
                        ).operations()
                    )
            except (OpenApiError, RefError, GraphQlIntrospectionError) as exc:
                raise ConfigError(f"upstream {key!r}: {exc}") from exc
    return introspected, resolved_base_urls
