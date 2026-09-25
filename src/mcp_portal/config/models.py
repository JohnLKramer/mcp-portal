"""Pydantic models for the main config. The published JSON Schema is generated
from these, so the schema cannot drift from the code that reads it.

P1 declares only fields P1 honors. Later phases add fields; additions are
backward-compatible, so `version: "1"` holds across phases.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    StringConstraints,
    Tag,
    model_validator,
)

from mcp_portal.naming import NAME_PATTERN
from mcp_portal.operations import BodyMode, Effect, ParamLocation, Sensitivity

SECRET_REF_PATTERN = r"^\$\{(env|file):[^}]+\}$"

SecretRef = Annotated[str, StringConstraints(pattern=SECRET_REF_PATTERN)]
"""Secret reference. Accepts only `${env:VAR}` or `${file:path}`; a literal
value fails validation, so a config stays safe to commit."""

ToolName = Annotated[str, StringConstraints(pattern=NAME_PATTERN.pattern)]
"""Explicit tool name. Held to the same character set and length limit as a
generated name, since an explicit name is published verbatim."""

# A schemeless host such as `api.example.com` loads fine and then fails on every
# call with httpx.UnsupportedProtocol. Requiring the scheme and a non-empty host
# keeps that a load-time error.
HTTP_URL_PATTERN = r"^https?://[^/?#\s]+"

BaseUrl = Annotated[str, StringConstraints(pattern=HTTP_URL_PATTERN)]
"""Absolute HTTP(S) base URL, kept as a plain string so path-joining stays
predictable."""


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenApiIntrospectionConfig(Base):
    url: BaseUrl | None = Field(default=None, description="OpenAPI document URL, for fetching over HTTP.")
    file: str | None = Field(default=None, description="OpenAPI document path, for loading from disk.")
    include_deprecated: bool = Field(default=False, description="Deprecated-operation inclusion flag.")
    allow_external_refs: bool = Field(default=False, description="External $ref following flag.")
    allowed_hosts: list[str] = Field(default_factory=list, description="External $ref host allowlist.")

    @model_validator(mode="after")
    def _exactly_one_document_source(self) -> Self:
        if (self.url is None) == (self.file is None):
            raise ValueError("introspection.openapi requires exactly one of 'url' or 'file'")
        return self


class GraphQlIntrospectionConfig(Base):
    url: BaseUrl
    # Field-tier exclusion, keyed by GraphQL type name. Only consulted when
    # generating a default selection set from introspection — a hand-authored
    # operations[] entry's document is never touched by this.
    type_policy: dict[str, list[str]] = Field(default_factory=dict)


class IntrospectionConfig(Base):
    openapi: OpenApiIntrospectionConfig | None = Field(default=None, description="OpenAPI introspection settings.")
    graphql: GraphQlIntrospectionConfig | None = Field(default=None, description="GraphQL introspection settings.")

    @model_validator(mode="after")
    def _exactly_one_protocol(self) -> Self:
        if (self.openapi is None) == (self.graphql is None):
            raise ValueError("introspection requires exactly one of 'openapi' or 'graphql'")
        return self


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class HttpServerConfig(Base):
    host: str = Field(default="127.0.0.1", description="Bind address.")
    port: int = Field(default=8443, gt=0, lt=65536, description="Bind port.")
    path: str = Field(default="/mcp", description="MCP endpoint path.")
    allowed_origins: list[str] = Field(
        default_factory=list,
        description="Allowed Origin header values, for browser cross-origin checks.",
    )
    # The `Host` header values real clients send, which need not be the bind
    # address: behind a reverse proxy, or bound to `0.0.0.0`, no client ever
    # sends `Host: 0.0.0.0`. Without this an operator using the sanctioned
    # non-loopback escape hatch has no way to name their public hostname, and
    # the SDK's DNS-rebinding defense rejects every request with a 421.
    allowed_hosts: list[str] = Field(
        default_factory=list,
        description=(
            "Allowed Host header values. Needed behind a reverse proxy or when "
            "bound to 0.0.0.0, since no client sends that as its Host header."
        ),
    )
    # Passed straight through to `Server.streamable_http_app`. `None` means
    # "use the SDK's own default" (`DEFAULT_SESSION_IDLE_TIMEOUT` /
    # `DEFAULT_MAX_SESSIONS`) rather than mcp-portal redeclaring those
    # numbers and risking drift from the SDK's.
    session_idle_timeout_s: float | None = Field(default=None, gt=0, allow_inf_nan=False, description="Session idle timeout, in seconds.")
    max_sessions: int | None = Field(default=None, gt=0, description="Maximum concurrent sessions.")


class OutboundConfig(Base):
    mode: Literal["none", "static", "client_credentials", "token_exchange"] = Field(
        default="none", description="Outbound credential strategy."
    )
    header: str = Field(default="Authorization", description="Credential header name.")
    scheme: str | None = Field(default="Bearer", description="Credential header scheme prefix.")
    value: SecretRef | None = Field(default=None, description="Static credential value, for mode 'static'.")
    token_endpoint: BaseUrl | None = Field(
        default=None,
        description="OAuth token endpoint, for mode 'client_credentials' or 'token_exchange'.",
    )
    client_id: str | None = Field(default=None, description="OAuth client ID.")
    client_secret: SecretRef | None = Field(default=None, description="OAuth client secret.")
    scopes: list[str] = Field(default_factory=list, description="Requested OAuth scopes.")
    audience: str | None = Field(default=None, description="Requested token audience.")
    resource: str | None = Field(default=None, description="Requested token resource indicator.")
    requested_token_type: str = Field(
        default="urn:ietf:params:oauth:token-type:access_token",
        description="Requested token type, for token exchange.",
    )

    @model_validator(mode="after")
    def _static_needs_a_value(self) -> Self:
        if self.mode == "static" and self.value is None:
            raise ValueError("outbound mode 'static' requires 'value'")
        return self

    @model_validator(mode="after")
    def _dynamic_modes_need_endpoint_and_client(self) -> Self:
        if self.mode not in ("client_credentials", "token_exchange"):
            return self
        missing = [
            name
            for name, value in (
                ("token_endpoint", self.token_endpoint),
                ("client_id", self.client_id),
                ("client_secret", self.client_secret),
            )
            if value is None
        ]
        if missing:
            raise ValueError(f"outbound mode {self.mode!r} requires {missing}")
        return self


class UpstreamAuthConfig(Base):
    outbound: OutboundConfig = Field(default_factory=OutboundConfig, description="Outbound credential settings.")


class UpstreamConfig(Base):
    protocol: Literal["http"] = Field(default="http", description="Upstream protocol.")
    base_url: BaseUrl | None = Field(default=None, description="Upstream base URL.")
    timeout_ms: int = Field(default=30000, gt=0, description="Per-call timeout, in milliseconds.")
    max_total_ms: int | None = Field(
        default=None,
        gt=0,
        description="Total retry budget, in milliseconds. Defaults to three times timeout_ms.",
    )
    max_response_bytes: int = Field(
        default=1024 * 1024,
        gt=0,
        description="Response size cap, in bytes. Larger responses get truncated first.",
    )
    auth: UpstreamAuthConfig = Field(default_factory=UpstreamAuthConfig, description="Upstream authentication settings.")
    introspection: IntrospectionConfig | None = Field(
        default=None,
        description=("OpenAPI introspection settings, for discovering tools instead of listing them."),
    )

    @model_validator(mode="after")
    def _default_total_budget(self) -> Self:
        if self.max_total_ms is None:
            object.__setattr__(self, "max_total_ms", self.timeout_ms * 3)
        return self

    @model_validator(mode="after")
    def _base_url_or_introspection(self) -> Self:
        if self.base_url is None and self.introspection is None:
            raise ValueError(
                "upstream requires either 'base_url' or 'introspection.openapi': "
                "there is otherwise no way to determine which server to call"
            )
        return self

    @model_validator(mode="after")
    def _graphql_introspection_needs_base_url(self) -> Self:
        if self.introspection is not None and self.introspection.graphql is not None and self.base_url is None:
            raise ValueError(
                "GraphQL introspection has no server-URL equivalent to OpenAPI's servers[]; "
                "'base_url' is required alongside 'introspection.graphql'"
            )
        return self

    @model_validator(mode="after")
    def _graphql_base_url_matches_introspection(self) -> Self:
        if (
            self.introspection is not None
            and self.introspection.graphql is not None
            and self.base_url is not None
            and self.base_url != self.introspection.graphql.url
        ):
            raise ValueError(
                "GraphQL runtime calls are made against 'base_url'; "
                "'introspection.graphql.url' must match it, or a mismatch means introspection "
                "succeeds but every real call will 404"
            )
        return self


class ServerConfig(Base):
    name: str = Field(description="Server name, reported to MCP clients.")
    transport: Literal["stdio", "http"] = Field(default="stdio", description="Transport protocol.")
    http: HttpServerConfig = Field(
        default_factory=HttpServerConfig,
        description="HTTP transport settings, used when transport is 'http'.",
    )


class ParameterEntry(Base):
    arg: str = Field(description="Tool argument name.")
    location: ParamLocation = Field(alias="in", description="Parameter location: query, path, or header.")
    wire_name: str | None = Field(default=None, description="Wire parameter name, if different from the argument name.")
    required: bool = Field(default=False, description="Required-parameter flag.")
    schema_: dict[str, Any] = Field(
        default_factory=lambda: {"type": "string"},
        alias="schema",
        description="Argument JSON Schema.",
    )
    # Only `form` is serialized. Declaring any other OpenAPI style would be
    # accepted and then silently ignored, so the type refuses it instead.
    style: Literal["form"] = Field(default="form", description="Parameter serialization style.")
    explode: bool = Field(default=True, description="Array/object explode flag.")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class BodyEntry(Base):
    content_type: str = Field(default="application/json", description="Request body content type.")
    mode: BodyMode = Field(default=BodyMode.FLATTEN, description="Body construction mode.")
    schema_: dict[str, Any] = Field(alias="schema", description="Request body JSON Schema.")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class HttpBindingEntry(Base):
    protocol: Literal["http"] = Field(default="http", description="Upstream call protocol.")
    method: str = Field(description="HTTP method.")
    path: str = Field(description="Upstream path.")
    parameters: list[ParameterEntry] = Field(default_factory=list, description="Bound parameters.")
    body: BodyEntry | None = Field(default=None, description="Bound request body.")


class GraphQlVariableEntry(Base):
    name: str
    graphql_type: str
    required: bool = False


class GraphQlBindingEntry(Base):
    protocol: Literal["graphql"] = "graphql"
    operation_type: Literal["query", "mutation"]
    document: str
    variables: list[GraphQlVariableEntry] = Field(default_factory=list)


def _binding_protocol(value: Any) -> str:
    # A plain Field(discriminator="protocol") requires the tag key to be
    # present in the input; every pre-GraphQL config on disk omits it and
    # relies on HttpBindingEntry's own `protocol: Literal["http"] = "http"`
    # default. A callable discriminator keeps that default working by
    # falling back to "http" when the key is absent, instead of turning
    # every un-annotated binding into a startup error.
    if isinstance(value, dict):
        return str(value.get("protocol", "http"))
    return str(getattr(value, "protocol", "http"))


BindingEntry = Annotated[
    Annotated[HttpBindingEntry, Tag("http")] | Annotated[GraphQlBindingEntry, Tag("graphql")],
    Discriminator(_binding_protocol),
]


class OperationEntry(Base):
    id: str = Field(description="Operation ID. The stable identifier for matching; never the tool name.")
    upstream: str = Field(description="Owning upstream name.")
    description: str = Field(description="Tool description, shown to the model.")
    title: str | None = Field(default=None, description="Tool title, shown to the model.")
    name: ToolName | None = Field(default=None, description="Explicit tool name override.")
    group_tags: list[str] = Field(default_factory=list, description="Grouping tags.")
    effect: Effect | None = Field(default=None, description="Operation effect override.")
    sensitivity: Sensitivity | None = Field(default=None, description="Operation sensitivity override.")
    binding: BindingEntry = Field(description="Upstream call binding.")


class MatchSpec(Base):
    ids: list[str] = Field(default_factory=list, description="Operation IDs to match.")
    tags: list[str] = Field(default_factory=list, description="Group tags to match.")
    upstream: list[str] = Field(default_factory=list, description="Upstream names to match.")
    effect: list[Effect] = Field(default_factory=list, description="Effects to match.")


class ClassificationRule(Base):
    match: MatchSpec = Field(description="Match criteria.")
    effect: Effect | None = Field(default=None, description="Effect to assign to matching operations.")
    sensitivity: Sensitivity | None = Field(default=None, description="Sensitivity to assign to matching operations.")


class SelectionConfig(Base):
    include_tags: list[str] | None = Field(default=None, description="Tags to expose. Omit to expose every tag.")
    exclude_tags: list[str] = Field(default_factory=list, description="Tags to hide.")
    include_ids: list[str] | None = Field(default=None, description="Operation IDs to expose. Omit to expose every operation.")
    exclude_ids: list[str] = Field(default_factory=list, description="Operation IDs to hide.")


class NamingConfig(Base):
    strategy: Literal["operation_id", "method_path"] = Field(default="operation_id", description="Tool name generation strategy.")
    prefix_with_group_tag: bool = Field(default=False, description="Group-tag prefix flag.")
    prefix_with_upstream: bool = Field(default=False, description="Upstream-name prefix flag.")


class LocalPrincipalConfig(Base):
    """Raw `authorization_details`, parsed into domain objects by
    `auth.principal.local_principal`. Kept as `list[dict]` here rather than
    `config.policy.RequireConfig`'s strict model: a *presented* detail may
    carry RFC 9396 type-specific extension fields the way a *required* one
    must not (§5) — strict rejection is reserved for `require`."""

    authorization_details: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Locally-asserted authorization details: the caller's identity outside "
            "the HTTP transport, or HTTP's fallback when inbound auth is disabled."
        ),
    )


class InboundAuthConfig(Base):
    """RFC 9068 access-token validation for `transport: http` (§8).

    `algorithms` intentionally has no way to re-admit `alg: none` — the
    validator below rejects it even if a future operator adds it by hand.
    HMAC (`HS*`) is not excluded outright (RFC 9396 does not forbid it and
    an operator's IdP might legitimately use it), but v1 has no
    shared-secret config field, so JWKS-only key lookup makes an
    HS*-allowlisted deployment fail closed rather than succeed via
    key-confusion.
    """

    enabled: bool = Field(default=False, description="Inbound JWT verification flag.")
    issuer: BaseUrl | None = Field(default=None, description="Expected token issuer.")
    audience: str | None = Field(default=None, description="Expected token audience.")
    jwks_uri: BaseUrl | None = Field(
        default=None,
        description="JWKS endpoint. Discovered automatically from the issuer when omitted.",
    )
    required_scopes: list[str] = Field(default_factory=list, description="Scopes every caller must have.")
    algorithms: list[str] = Field(
        default_factory=lambda: ["RS256", "ES256"],
        description="Accepted JWT signing algorithms.",
    )
    leeway_s: float = Field(default=60.0, ge=0, description="Clock-skew allowance, in seconds.")
    allow_unauthenticated_http: bool = Field(
        default=False,
        description=("Unauthenticated HTTP escape hatch, for non-loopback hosts with no inbound auth."),
    )

    @model_validator(mode="after")
    def _enabled_needs_issuer_and_audience(self) -> Self:
        if self.enabled and (self.issuer is None or self.audience is None):
            raise ValueError("auth.inbound.enabled requires 'issuer' and 'audience'")
        return self

    @model_validator(mode="after")
    def _none_alg_never_allowed(self) -> Self:
        if "none" in self.algorithms:
            raise ValueError("'none' may never appear in auth.inbound.algorithms")
        return self


class AuthConfig(Base):
    local_principal: LocalPrincipalConfig = Field(default_factory=LocalPrincipalConfig, description="Locally-asserted principal settings.")
    inbound: InboundAuthConfig = Field(default_factory=InboundAuthConfig, description="Inbound JWT verification settings.")


class PolicyFileConfig(Base):
    file: str = Field(description="Policy file path.")


class McpPortalConfig(Base):
    version: Literal["1"] = Field(description="Config schema version.")
    mode: Literal["configured", "introspect-safe", "introspect-unsafe"] = Field(description="Tool source mode.")
    acknowledge_unsafe: bool = Field(
        default=False,
        description="Unsafe-introspection acknowledgement flag, required for 'introspect-unsafe'.",
    )
    server: ServerConfig = Field(description="Server settings.")
    upstreams: dict[str, UpstreamConfig] = Field(description="Upstreams by name.")
    operations: list[OperationEntry] = Field(default_factory=list, description="Hand-configured operations.")
    classification: list[ClassificationRule] = Field(default_factory=list, description="Classification rules.")
    selection: SelectionConfig = Field(default_factory=SelectionConfig, description="Tool selection settings.")
    naming: NamingConfig = Field(default_factory=NamingConfig, description="Tool naming settings.")
    auth: AuthConfig = Field(default_factory=AuthConfig, description="Authentication settings.")
    policy: PolicyFileConfig | None = Field(default=None, description="Policy file reference.")

    @model_validator(mode="after")
    def _operations_reference_known_upstreams(self) -> Self:
        unknown = sorted({o.upstream for o in self.operations} - set(self.upstreams))
        if unknown:
            raise ValueError(f"operations reference unknown upstreams: {unknown}")
        return self

    @model_validator(mode="after")
    def _unsafe_mode_requires_acknowledgement(self) -> Self:
        if self.mode == "introspect-unsafe" and not self.acknowledge_unsafe:
            raise ValueError(
                "mode 'introspect-unsafe' requires 'acknowledge_unsafe: true'; selecting "
                "the mode alone must not be enough to expose every discovered operation"
            )
        return self

    @model_validator(mode="after")
    def _configured_mode_needs_every_base_url(self) -> Self:
        if self.mode == "configured":
            missing = sorted(k for k, u in self.upstreams.items() if u.base_url is None)
            if missing:
                raise ValueError(
                    f"upstream(s) {missing} have no 'base_url'; mode 'configured' never "
                    "introspects, so it can never be resolved from a document"
                )
        return self

    @model_validator(mode="after")
    def _unauthenticated_http_is_guarded(self) -> Self:
        if self.server.transport != "http" or self.auth.inbound.enabled:
            return self
        if self.server.http.host in LOOPBACK_HOSTS:
            return self
        if self.auth.inbound.allow_unauthenticated_http:
            return self
        raise ValueError(
            "transport 'http' with auth.inbound.enabled=false on a non-loopback host "
            "is a startup error unless auth.inbound.allow_unauthenticated_http is "
            "true: it is an unauthenticated endpoint holding a service credential (§8)"
        )

    @model_validator(mode="after")
    def _token_exchange_requires_inbound(self) -> Self:
        exchanging = sorted(k for k, u in self.upstreams.items() if u.auth.outbound.mode == "token_exchange")
        if exchanging and not (self.server.transport == "http" and self.auth.inbound.enabled):
            raise ValueError(
                f"upstream(s) {exchanging} use outbound mode 'token_exchange', which "
                "requires transport 'http' with auth.inbound.enabled=true: there is no "
                "subject_token to exchange otherwise"
            )
        return self
