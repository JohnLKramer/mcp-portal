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
"""A secret-typed field. Only ${env:VAR} and ${file:/path} are permitted.

A literal fails validation rather than warning, so configs are safe to commit by
construction rather than by discipline.
"""

ToolName = Annotated[str, StringConstraints(pattern=NAME_PATTERN.pattern)]
"""An explicitly declared tool name. Held to what generate_names would produce.

An explicit name is published verbatim, so it gets the same character set and
length cap a generated one does rather than being trusted because an operator
typed it.
"""

# A schemeless host such as `api.example.com` loads fine and then fails on every
# call with httpx.UnsupportedProtocol. Requiring the scheme and a non-empty host
# keeps that a load-time error.
HTTP_URL_PATTERN = r"^https?://[^/?#\s]+"

BaseUrl = Annotated[str, StringConstraints(pattern=HTTP_URL_PATTERN)]
"""An absolute http(s) base URL. Kept a `str` so path joining stays unsurprising."""


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenApiIntrospectionConfig(Base):
    url: BaseUrl | None = None
    file: str | None = None
    include_deprecated: bool = False
    allow_external_refs: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)

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
    openapi: OpenApiIntrospectionConfig | None = None
    graphql: GraphQlIntrospectionConfig | None = None

    @model_validator(mode="after")
    def _exactly_one_protocol(self) -> Self:
        if (self.openapi is None) == (self.graphql is None):
            raise ValueError("introspection requires exactly one of 'openapi' or 'graphql'")
        return self


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class HttpServerConfig(Base):
    host: str = "127.0.0.1"
    port: int = Field(default=8443, gt=0, lt=65536)
    path: str = "/mcp"
    allowed_origins: list[str] = Field(default_factory=list)
    # The `Host` header values real clients send, which need not be the bind
    # address: behind a reverse proxy, or bound to `0.0.0.0`, no client ever
    # sends `Host: 0.0.0.0`. Without this an operator using the sanctioned
    # non-loopback escape hatch has no way to name their public hostname, and
    # the SDK's DNS-rebinding defense rejects every request with a 421.
    allowed_hosts: list[str] = Field(default_factory=list)


class OutboundConfig(Base):
    mode: Literal["none", "static", "client_credentials", "token_exchange"] = "none"
    header: str = "Authorization"
    scheme: str | None = "Bearer"
    value: SecretRef | None = None
    token_endpoint: BaseUrl | None = None
    client_id: str | None = None
    client_secret: SecretRef | None = None
    scopes: list[str] = Field(default_factory=list)
    audience: str | None = None
    resource: str | None = None
    requested_token_type: str = "urn:ietf:params:oauth:token-type:access_token"

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
    outbound: OutboundConfig = Field(default_factory=OutboundConfig)


class UpstreamConfig(Base):
    protocol: Literal["http"] = "http"
    base_url: BaseUrl | None = None
    timeout_ms: int = Field(default=30000, gt=0)
    max_total_ms: int | None = Field(default=None, gt=0)
    max_response_bytes: int = Field(default=1024 * 1024, gt=0)
    auth: UpstreamAuthConfig = Field(default_factory=UpstreamAuthConfig)
    introspection: IntrospectionConfig | None = None

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
        if (
            self.introspection is not None
            and self.introspection.graphql is not None
            and self.base_url is None
        ):
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
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    http: HttpServerConfig = Field(default_factory=HttpServerConfig)


class ParameterEntry(Base):
    arg: str
    location: ParamLocation = Field(alias="in")
    wire_name: str | None = None
    required: bool = False
    schema_: dict[str, Any] = Field(default_factory=lambda: {"type": "string"}, alias="schema")
    # Only `form` is serialized. Declaring any other OpenAPI style would be
    # accepted and then silently ignored, so the type refuses it instead.
    style: Literal["form"] = "form"
    explode: bool = True

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class BodyEntry(Base):
    content_type: str = "application/json"
    mode: BodyMode = BodyMode.FLATTEN
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class HttpBindingEntry(Base):
    protocol: Literal["http"] = "http"
    method: str
    path: str
    parameters: list[ParameterEntry] = Field(default_factory=list)
    body: BodyEntry | None = None


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
    id: str
    upstream: str
    description: str
    title: str | None = None
    name: ToolName | None = None
    group_tags: list[str] = Field(default_factory=list)
    effect: Effect | None = None
    sensitivity: Sensitivity | None = None
    binding: BindingEntry


class MatchSpec(Base):
    ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    upstream: list[str] = Field(default_factory=list)
    effect: list[Effect] = Field(default_factory=list)


class ClassificationRule(Base):
    match: MatchSpec
    effect: Effect | None = None
    sensitivity: Sensitivity | None = None


class SelectionConfig(Base):
    include_tags: list[str] | None = None
    exclude_tags: list[str] = Field(default_factory=list)
    include_ids: list[str] | None = None
    exclude_ids: list[str] = Field(default_factory=list)


class NamingConfig(Base):
    strategy: Literal["operation_id", "method_path"] = "operation_id"
    prefix_with_group_tag: bool = False
    prefix_with_upstream: bool = False


class LocalPrincipalConfig(Base):
    """Raw `authorization_details`, parsed into domain objects by
    `auth.principal.local_principal`. Kept as `list[dict]` here rather than
    `config.policy.RequireConfig`'s strict model: a *presented* detail may
    carry RFC 9396 type-specific extension fields the way a *required* one
    must not (§5) — strict rejection is reserved for `require`."""

    authorization_details: list[dict[str, Any]] = Field(default_factory=list)


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

    enabled: bool = False
    issuer: BaseUrl | None = None
    audience: str | None = None
    jwks_uri: BaseUrl | None = None
    required_scopes: list[str] = Field(default_factory=list)
    algorithms: list[str] = Field(default_factory=lambda: ["RS256", "ES256"])
    leeway_s: float = Field(default=60.0, ge=0)
    allow_unauthenticated_http: bool = False

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
    local_principal: LocalPrincipalConfig = Field(default_factory=LocalPrincipalConfig)
    inbound: InboundAuthConfig = Field(default_factory=InboundAuthConfig)


class PolicyFileConfig(Base):
    file: str


class Config(Base):
    version: Literal["1"]
    mode: Literal["configured", "introspect-safe", "introspect-unsafe"]
    acknowledge_unsafe: bool = False
    server: ServerConfig
    upstreams: dict[str, UpstreamConfig]
    operations: list[OperationEntry] = Field(default_factory=list)
    classification: list[ClassificationRule] = Field(default_factory=list)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    naming: NamingConfig = Field(default_factory=NamingConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    policy: PolicyFileConfig | None = None

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
        exchanging = sorted(
            k for k, u in self.upstreams.items() if u.auth.outbound.mode == "token_exchange"
        )
        if exchanging and not (self.server.transport == "http" and self.auth.inbound.enabled):
            raise ValueError(
                f"upstream(s) {exchanging} use outbound mode 'token_exchange', which "
                "requires transport 'http' with auth.inbound.enabled=true: there is no "
                "subject_token to exchange otherwise"
            )
        return self
