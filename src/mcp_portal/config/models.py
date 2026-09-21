"""Pydantic models for the main config. The published JSON Schema is generated
from these, so the schema cannot drift from the code that reads it.

P1 declares only fields P1 honors. Later phases add fields; additions are
backward-compatible, so `version: "1"` holds across phases.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

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


class IntrospectionConfig(Base):
    openapi: OpenApiIntrospectionConfig


class OutboundConfig(Base):
    mode: Literal["none", "static"] = "none"
    header: str = "Authorization"
    scheme: str | None = "Bearer"
    value: SecretRef | None = None

    @model_validator(mode="after")
    def _static_needs_a_value(self) -> Self:
        if self.mode == "static" and self.value is None:
            raise ValueError("outbound mode 'static' requires 'value'")
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


class ServerConfig(Base):
    name: str
    transport: Literal["stdio"] = "stdio"


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


class BindingEntry(Base):
    protocol: Literal["http"] = "http"
    method: str
    path: str
    parameters: list[ParameterEntry] = Field(default_factory=list)
    body: BodyEntry | None = None


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


class AuthConfig(Base):
    local_principal: LocalPrincipalConfig = Field(default_factory=LocalPrincipalConfig)


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
