"""Wire a loaded config into a runnable application."""

import logging
from dataclasses import dataclass

import httpx

from mcp_portal.auth.outbound import ClientCredentialsSource, TokenExchangeSource, credential_for
from mcp_portal.auth.principal import Principal, local_principal
from mcp_portal.auth.token_cache import TokenCache
from mcp_portal.config.loader import (
    ConfigError,
    LoadedConfig,
)
from mcp_portal.config.policy import PolicyConfig, PolicyDefaults
from mcp_portal.introspect import introspect_upstreams
from mcp_portal.naming import NameCollisionError
from mcp_portal.operations import Effect, Operation
from mcp_portal.policy import PolicyEngine
from mcp_portal.registry import build_toolset
from mcp_portal.server.mcp import ToolInvoker
from mcp_portal.sources.explicit import ExplicitSource
from mcp_portal.sources.merge import merge_operations
from mcp_portal.transports.http import CredentialSource, HttpTransport, StaticCredentialSource

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

    introspected: list[Operation] = []
    resolved_base_urls = {key: u.base_url or "" for key, u in config.upstreams.items()}
    if config.mode != "configured":
        introspected, resolved_base_urls = introspect_upstreams(config, loaded.base_dir)

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

    policy_config = loaded.policy or PolicyConfig(version="1", defaults=PolicyDefaults())
    policy = PolicyEngine(policy_config)
    for warning in policy.dead_rule_warnings(toolset.operations):
        log.warning("%s", warning)

    if config.server.transport == "stdio":
        principal = local_principal(config.auth.local_principal)
        # Guardrail, not a security boundary (§8): anyone who can launch this
        # process can already edit the config or call the upstream directly.
        log.info(
            "local principal in effect for stdio: %d self-asserted authorization "
            "detail(s); %d policy rule(s) active. This is a guardrail against an "
            "over-eager agent, not a security boundary.",
            len(principal.authorization_details),
            len(policy_config.rules),
        )
    else:
        # Under `transport: http` every call resolves its own principal per
        # request (`server/http.py`'s `_principal_for_request`, which builds the
        # local principal itself when inbound auth is disabled and denies
        # outright when it is enabled but no token is present). The invoker's
        # instance default is therefore only ever reached by a caller that
        # forgot the keyword, so it carries no authority. `server/http.py` logs
        # the posture banner for the http path; nothing to log here.
        principal = Principal("local", ())

    token_cache = TokenCache()
    clients: list[httpx.AsyncClient] = []
    transports = {}
    for key, upstream in config.upstreams.items():
        client = httpx.AsyncClient()
        clients.append(client)
        outbound = upstream.auth.outbound
        credential_source: CredentialSource
        if outbound.mode == "client_credentials":
            credential_source = ClientCredentialsSource(
                outbound=outbound,
                secrets=loaded.secrets,
                client=client,
                cache=token_cache,
                upstream_key=key,
            )
        elif outbound.mode == "token_exchange":
            assert outbound.token_endpoint is not None
            assert outbound.client_id is not None
            assert outbound.client_secret is not None
            client_secret = loaded.secrets.get(outbound.client_secret)
            if client_secret is None:
                raise ConfigError(
                    f"secret reference {outbound.client_secret!r} was not resolved at load time"
                )
            credential_source = TokenExchangeSource(
                token_endpoint=outbound.token_endpoint,
                client_id=outbound.client_id,
                client_secret=client_secret,
                audience=outbound.audience,
                resource=outbound.resource,
                requested_token_type=outbound.requested_token_type,
                scopes=outbound.scopes,
                client=client,
                cache=token_cache,
                upstream_key=key,
            )
        else:
            credential_source = StaticCredentialSource(credential_for(outbound, loaded.secrets))
        transports[key] = HttpTransport(
            client=client,
            upstream=upstream.model_copy(update={"base_url": resolved_base_urls[key]}),
            credential_source=credential_source,
        )

    log.info("serving %d tool(s) from %d upstream(s)", len(toolset.operations), len(transports))
    return App(
        invoker=ToolInvoker(toolset, transports, policy=policy, principal=principal),
        warnings=toolset.warnings,
        _clients=clients,
    )
