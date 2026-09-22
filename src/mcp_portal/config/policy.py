"""Pydantic models for the RAR policy file (§5 of the design).

Kept in its own module, loaded from its own file: the main config's content
is derived in introspection mode and regenerated whenever the upstream
document changes, but authorization policy must never be silently
regenerated from a third party's schema.
"""

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from mcp_portal.config.models import Base
from mcp_portal.operations import Effect, Sensitivity


class AuthorizationDetailRequirement(Base):
    """One required RFC 9396 detail. `extra="forbid"` (inherited from `Base`)
    is the single most important parsing decision in the contract: under
    Pydantic's default behavior an unknown field like a mistyped `privileges`
    would parse cleanly and enforce nothing, silently making the rule broader
    than written in the one file that must fail closed."""

    type: str
    actions: list[str] | None = None
    locations: list[str] | None = None
    datatypes: list[str] | None = None
    identifier: str | None = None
    privileges: list[str] | None = None

    @field_validator("actions", "locations", "datatypes", "privileges")
    @classmethod
    def _non_empty_when_present(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) == 0:
            raise ValueError(
                "must be omitted or non-empty: an empty array requires nothing (∅ ⊆ "
                "anything) while appearing to require something"
            )
        return value


class RequireConfig(Base):
    authorization_details: list[AuthorizationDetailRequirement]

    @field_validator("authorization_details")
    @classmethod
    def _non_empty(
        cls, value: list[AuthorizationDetailRequirement]
    ) -> list[AuthorizationDetailRequirement]:
        if len(value) == 0:
            raise ValueError(
                "'require.authorization_details' must not be empty; an empty 'require' "
                "requires nothing and should be omitted instead"
            )
        return value


class PolicyOutboundConfig(Base):
    carry: bool = False


class PolicyMatchSpec(Base):
    """Same match vocabulary as `registry.MatchSpec`, plus `sensitivity`.

    There is deliberately no `name`: names are unstable by construction
    (naming runs after selection), so a policy rule keyed on one would fail
    open the moment a collision suffix appears.
    """

    ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    upstream: list[str] = Field(default_factory=list)
    effect: list[Effect] = Field(default_factory=list)
    sensitivity: list[Sensitivity] = Field(default_factory=list)


class PolicyRule(Base):
    match: PolicyMatchSpec
    require: RequireConfig | None = None
    outbound: PolicyOutboundConfig = Field(default_factory=PolicyOutboundConfig)


class PolicyDefaults(Base):
    unmatched: Literal["allow", "deny"] = "allow"


class PolicyConfig(Base):
    version: Literal["1"]
    defaults: PolicyDefaults = Field(default_factory=PolicyDefaults)
    rules: list[PolicyRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_less_rule_forbidden_under_deny(self) -> Self:
        if self.defaults.unmatched != "deny":
            return self
        offending = [i for i, rule in enumerate(self.rules) if rule.require is None]
        if offending:
            raise ValueError(
                f"rule(s) at index {offending} have no 'require' while "
                "'defaults.unmatched' is 'deny': a require-less rule would flip "
                "matched-but-unrequired operations from denied to callable, which is "
                "a loosening the accumulation model must never permit"
            )
        return self
