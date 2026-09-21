"""Wire a loaded config into a runnable application."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from mcp_portal.auth.outbound import credential_for
from mcp_portal.config.loader import (
    ConfigError,
    LoadedConfig,
    credential_headers_for,
)
from mcp_portal.config.models import HTTP_URL_PATTERN, Config
from mcp_portal.naming import NameCollisionError
from mcp_portal.operations import Effect, Operation
from mcp_portal.registry import build_toolset
from mcp_portal.server.mcp import ToolInvoker
from mcp_portal.sources.explicit import ExplicitSource
from mcp_portal.sources.merge import merge_operations
from mcp_portal.sources.openapi import OpenApiSource, load_document
from mcp_portal.sources.openapi_document import OpenApiError
from mcp_portal.sources.refs import RefError
from mcp_portal.transports.http import HttpTransport

log = logging.getLogger("mcp_portal")


@dataclass(slots=True)
class App:
    invoker: ToolInvoker
    warnings: tuple[str, ...]
    _clients: list[httpx.AsyncClient]

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()


def _introspect(config: Config, base_dir: Path) -> tuple[list[Operation], dict[str, str]]:
    """Fetch and parse every upstream's OpenAPI document, when configured.

    Returns the introspected operations and each upstream's resolved base URL —
    `base_url` when the operator set one, otherwise the document's own `servers[]`.
    """
    introspected: list[Operation] = []
    resolved_base_urls: dict[str, str] = {}
    with httpx.Client() as client:
        for key, upstream in config.upstreams.items():
            resolved_base_urls[key] = upstream.base_url or ""
            if upstream.introspection is None:
                continue
            try:
                loaded = load_document(
                    upstream.introspection.openapi, base_dir, upstream.base_url, client
                )
                for warning in loaded.warnings:
                    log.warning("upstream %r: %s", key, warning)

                resolved = upstream.base_url or loaded.base_url
                # `resolve_base_url` returns whatever the document's servers[] says,
                # unvalidated — `model_copy` below skips pydantic validation entirely,
                # so this is the only place a malformed (e.g. relative) document-derived
                # URL is caught at load time instead of failing every call at runtime.
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
            except (OpenApiError, RefError) as exc:
                raise ConfigError(f"upstream {key!r}: {exc}") from exc
    return introspected, resolved_base_urls


def build_app(loaded: LoadedConfig) -> App:
    config = loaded.config

    introspected: list[Operation] = []
    resolved_base_urls = {key: u.base_url or "" for key, u in config.upstreams.items()}
    if config.mode != "configured":
        introspected, resolved_base_urls = _introspect(config, loaded.base_dir)

    explicit = list(ExplicitSource(config).operations())

    # All three steps can raise NameCollisionError — merge_operations rejects a
    # duplicate id within operations[] itself (the same load error P1 always
    # raised; merging must not let it silently become last-write-wins), and
    # build_toolset still catches the introspected-vs-explicit case.
    try:
        operations = merge_operations(introspected, explicit)
        toolset = build_toolset(operations, config)
    except NameCollisionError as exc:
        # naming/ stays free of config imports, so the translation happens here —
        # a duplicate id is a config problem and must exit like every other one.
        raise ConfigError(str(exc)) from exc

    for warning in toolset.warnings:
        log.warning("%s", warning)

    if config.mode == "introspect-unsafe":
        risky = sorted(op.name for op in toolset.operations if op.effect is not Effect.READ_ONLY)
        log.warning(
            "mode 'introspect-unsafe': exposing %d state-changing tool(s): %s",
            len(risky),
            ", ".join(risky) or "(none)",
        )

    clients: list[httpx.AsyncClient] = []
    transports = {}
    for key, upstream in config.upstreams.items():
        client = httpx.AsyncClient()
        clients.append(client)
        transports[key] = HttpTransport(
            client=client,
            upstream=upstream.model_copy(update={"base_url": resolved_base_urls[key]}),
            credential=credential_for(upstream.auth.outbound, loaded.secrets),
        )

    log.info("serving %d tool(s) from %d upstream(s)", len(toolset.operations), len(transports))
    return App(
        invoker=ToolInvoker(toolset, transports),
        warnings=toolset.warnings,
        _clients=clients,
    )
