"""Wire a loaded config into a runnable application."""

import logging
from dataclasses import dataclass

import httpx

from mcp_portal.auth.outbound import credential_for
from mcp_portal.config.loader import ConfigError, LoadedConfig
from mcp_portal.naming import NameCollisionError
from mcp_portal.registry import build_toolset
from mcp_portal.server.mcp import ToolInvoker
from mcp_portal.sources.explicit import ExplicitSource
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


def build_app(loaded: LoadedConfig) -> App:
    config = loaded.config

    operations = list(ExplicitSource(config).operations())
    try:
        toolset = build_toolset(operations, config)
    except NameCollisionError as exc:
        # naming/ stays free of config imports, so the translation happens here —
        # a duplicate id is a config problem and must exit like every other one.
        raise ConfigError(str(exc)) from exc

    for warning in toolset.warnings:
        log.warning("%s", warning)

    clients: list[httpx.AsyncClient] = []
    transports = {}
    for key, upstream in config.upstreams.items():
        client = httpx.AsyncClient()
        clients.append(client)
        transports[key] = HttpTransport(
            client=client,
            upstream=upstream,
            credential=credential_for(upstream.auth.outbound, loaded.secrets),
        )

    log.info("serving %d tool(s) from %d upstream(s)", len(toolset.operations), len(transports))
    return App(
        invoker=ToolInvoker(toolset, transports),
        warnings=toolset.warnings,
        _clients=clients,
    )
