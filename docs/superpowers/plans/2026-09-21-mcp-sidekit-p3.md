# mcp-portal P3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add RAR (Rich Authorization Requests, RFC 9396) policy enforcement to
every tool invocation, evaluated against a locally-asserted principal, working
under `transport: stdio` in every mode (`configured`, `introspect-safe`,
`introspect-unsafe`).

**Architecture:** A separate policy file (`config/policy.py`) parsed
independently of the main config, so hand-written authorization policy
survives re-introspection. `auth/rar.py` is the pure coverage predicate:
"does this presented `authorization_details` set satisfy this required
detail." `auth/principal.py` turns `auth.local_principal` config into a
`Principal`. `policy.py` composes both: it matches rules against an
`Operation` (reusing the `id`/`tags`/`upstream`/`effect` match vocabulary
`registry.py` already uses, extended with `sensitivity`), accumulates their
`require`d details, and calls `auth/rar.covers` per requirement to reach an
allow/deny decision. `server/mcp.py`'s `ToolInvoker` calls the engine between
argument validation and transport execution — the same point §9 of the design
puts it — so a denial never reaches the upstream. Outbound credential
*carrying* (`outbound.carry`) is computed by the engine now, per the design's
"a phase that publishes a config field must implement it" rule, but nothing
consumes it yet: the only outbound modes that exist until P4 (`none`,
`static`) do not vary per call, so there is nothing for a carried detail to
attach to.

**Tech Stack:** Same as P1/P2 (Python 3.14.7, uv, Pydantic v2, httpx,
`mcp` SDK, ruamel.yaml, jsonschema, pytest, ruff, mypy). No new dependencies.

**Spec:** [`docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md`](../specs/2026-09-19-mcp-sidekit-design.md)

## Global Constraints

- **Scope is P3 only.** Not in this plan: `auth/inbound.py` (JWT validation,
  JWKS, RFC 9728 discovery), `auth/outbound.py`'s `client_credentials` /
  `token_exchange` modes and the token cache, `server/http.py`, and
  `configure/`. Do not add config fields for them. `auth.inbound` is **not**
  added to `AuthConfig` in this plan — it is a P4 field.
- **`policy` is optional on the main config.** A config with no `policy` key
  behaves exactly as P1/P2 configs already do: every call is allowed. This is
  what keeps `version: "1"` backward-compatible across phases.
- **`auth.local_principal` is optional too**, defaulting to empty
  `authorization_details`. Per §8 of the design, `transport: stdio` always
  uses the local principal — there is no other principal source until P4
  adds inbound HTTP.
- **`require` is parsed with `extra="forbid"`.** An unknown field
  (`privileges` mis-typed, a made-up field) is a load error, never a
  silently-ignored one. This is, verbatim, "the single most important parsing
  decision in the contract" (§5).
- **Empty arrays are rejected** in `require.authorization_details[]`'s
  `actions`/`locations`/`datatypes`/`privileges`, and the top-level
  `authorization_details` list itself must be non-empty when `require` is
  present. `∅ ⊆ anything` requires nothing while appearing to require
  something.
- **A `require`-less rule under `defaults.unmatched: deny` is a load error**,
  not a runtime surprise. Otherwise a carry-only rule silently flips an
  operation from denied to callable-with-no-requirements — a loosening.
- **Only `require`-bearing rules count as "matched."** A carry-only rule
  contributes `outbound.carry` but never satisfies `defaults.unmatched: deny`
  on its own.
- **Policy rules accumulate; they are never first-match-wins.** The effective
  requirement for an operation is the union of every matching rule's
  `require`.
- **RAR coverage requires a single presented detail to satisfy a required
  one.** Composing coverage across multiple presented details is explicitly
  out of scope — it would let two narrow grants combine into an authority
  neither one conveyed.
- **Location matching is exact string equality.** Hierarchical/prefix
  matching is deferred (§8): the wrong answer there is a privilege
  escalation.
- **`match.name` does not exist**, in policy exactly as in `selection`
  (`registry.py`). Names are unstable by construction (naming runs after
  selection); a policy rule keyed on one would fail open the moment a
  collision suffix appears.
- **A policy rule matching zero operations logs a warning at startup**, not
  an error — mirrors `registry._select`'s dead-rule warning.
- **`policy.file` resolves relative to the directory containing the main
  config file**, not the process CWD (same rule P1 already applies to
  `${file:}`).
- **The local-principal guardrail is a guardrail, not a security boundary.**
  Startup logs must say so in those words: anyone who can launch the stdio
  process can already edit the config or call the upstream directly.
- Every task ends with `uv run ruff format .`, `uv run ruff check .`,
  `uv run mypy src`, and the task's own test file passing before the commit
  step. Do not run the full suite mid-task; the final task is where
  everything runs together.

---

### Task 1: RAR coverage predicate and claim parsing (`auth/rar.py`)

**Files:**
- Create: `src/mcp_portal/auth/rar.py`
- Create: `tests/test_auth_rar.py`

**Interfaces:**
- Consumes: nothing from other P3 modules — pure, no config imports, so it is
  testable and reusable unchanged by P4's inbound JWT claim parsing.
- Produces: `RarError`, `AuthorizationDetail` (frozen dataclass: `type: str`,
  `actions: tuple[str, ...] = ()`, `locations: tuple[str, ...] = ()`,
  `datatypes: tuple[str, ...] = ()`, `identifier: str | None = None`,
  `privileges: tuple[str, ...] = ()`), `covers(required, presented) -> bool`,
  `parse_authorization_details(raw: object) -> tuple[AuthorizationDetail, ...]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth_rar.py`:

```python
import pytest

from mcp_portal.auth.rar import (
    AuthorizationDetail,
    RarError,
    covers,
    parse_authorization_details,
)


def detail(**overrides) -> AuthorizationDetail:
    base = {
        "type": "payment_initiation",
        "actions": (),
        "locations": (),
        "datatypes": (),
        "identifier": None,
        "privileges": (),
    }
    return AuthorizationDetail(**(base | overrides))


def test_a_detail_with_matching_type_and_no_other_fields_is_covered():
    required = detail()
    presented = detail()
    assert covers(required, [presented]) is True


def test_a_detail_with_a_different_type_is_never_covered():
    required = detail(type="payment_initiation")
    presented = detail(type="account_access")
    assert covers(required, [presented]) is False


def test_required_actions_must_be_a_subset_of_presented_actions():
    required = detail(actions=("initiate",))
    assert covers(required, [detail(actions=("initiate", "cancel"))]) is True
    assert covers(required, [detail(actions=("cancel",))]) is False


def test_required_locations_match_by_exact_string_equality():
    required = detail(locations=("https://api.example.com/v1/payments",))
    assert covers(required, [detail(locations=("https://api.example.com/v1/payments",))]) is True
    # A prefix is not equality: hierarchical matching is deliberately deferred.
    assert covers(required, [detail(locations=("https://api.example.com/v1",))]) is False


def test_required_datatypes_must_be_a_subset():
    required = detail(datatypes=("balance",))
    assert covers(required, [detail(datatypes=("balance", "history"))]) is True
    assert covers(required, [detail(datatypes=("history",))]) is False


def test_required_privileges_must_be_a_subset():
    required = detail(privileges=("admin",))
    assert covers(required, [detail(privileges=("admin", "read"))]) is True
    assert covers(required, [detail(privileges=("read",))]) is False


def test_required_identifier_must_equal_presented_identifier():
    required = detail(identifier="acct-1")
    assert covers(required, [detail(identifier="acct-1")]) is True
    assert covers(required, [detail(identifier="acct-2")]) is False


def test_a_field_the_required_detail_does_not_specify_is_not_checked():
    # Presented has extra privileges the requirement never asked about.
    required = detail(type="payment_initiation", actions=("initiate",))
    presented = detail(type="payment_initiation", actions=("initiate",), privileges=("admin",))
    assert covers(required, [presented]) is True


def test_a_field_the_required_detail_specifies_but_presented_lacks_fails_coverage():
    required = detail(identifier="acct-1")
    presented = detail(identifier=None)
    assert covers(required, [presented]) is False


def test_coverage_must_come_from_a_single_presented_detail_not_a_composition():
    required = detail(actions=("initiate",), locations=("https://api.example.com/pay",))
    presented = [
        detail(actions=("initiate",)),
        detail(locations=("https://api.example.com/pay",)),
    ]
    assert covers(required, presented) is False


def test_covers_with_no_presented_details_is_false():
    assert covers(detail(), []) is False


def test_parse_authorization_details_accepts_a_well_formed_list():
    raw = [
        {
            "type": "payment_initiation",
            "actions": ["initiate"],
            "locations": ["https://api.example.com/pay"],
            "identifier": "acct-1",
        }
    ]
    (parsed,) = parse_authorization_details(raw)
    assert parsed.type == "payment_initiation"
    assert parsed.actions == ("initiate",)
    assert parsed.locations == ("https://api.example.com/pay",)
    assert parsed.identifier == "acct-1"


def test_parse_authorization_details_defaults_missing_optional_fields():
    (parsed,) = parse_authorization_details([{"type": "account_access"}])
    assert parsed.actions == ()
    assert parsed.datatypes == ()
    assert parsed.privileges == ()
    assert parsed.identifier is None


def test_parse_authorization_details_empty_list_is_fine():
    assert parse_authorization_details([]) == ()


def test_parse_authorization_details_rejects_a_non_list():
    with pytest.raises(RarError):
        parse_authorization_details({"type": "x"})


def test_parse_authorization_details_rejects_a_non_object_entry():
    with pytest.raises(RarError):
        parse_authorization_details(["not-an-object"])


def test_parse_authorization_details_rejects_a_missing_type():
    with pytest.raises(RarError) as exc:
        parse_authorization_details([{"actions": ["initiate"]}])
    assert "type" in str(exc.value)


def test_parse_authorization_details_rejects_a_non_string_type():
    with pytest.raises(RarError):
        parse_authorization_details([{"type": 5}])


def test_parse_authorization_details_rejects_a_non_list_actions_field():
    with pytest.raises(RarError):
        parse_authorization_details([{"type": "x", "actions": "initiate"}])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_auth_rar.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.auth.rar'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/auth/rar.py`:

```python
"""RFC 9396 Rich Authorization Requests: the coverage predicate and claim parsing.

This module never learns where an `authorization_details` set came from — a
JWT claim, a local config list — so it is shared unchanged between the local
principal (P3) and the inbound JWT principal (P4).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


class RarError(Exception):
    """Raised when an `authorization_details` value is malformed.

    Per §8 of the design, a malformed claim must deny the request rather than
    be treated as absent — callers translate this into a denial or, for a
    config-sourced principal, a startup `ConfigError`.
    """


@dataclass(frozen=True, slots=True)
class AuthorizationDetail:
    """One RFC 9396 authorization detail, either required or presented."""

    type: str
    actions: tuple[str, ...] = field(default=())
    locations: tuple[str, ...] = field(default=())
    datatypes: tuple[str, ...] = field(default=())
    identifier: str | None = None
    privileges: tuple[str, ...] = field(default=())


def covers(required: AuthorizationDetail, presented: Iterable[AuthorizationDetail]) -> bool:
    """Is `required` satisfied by a single detail in `presented`? (§8)

    Rules apply only to fields `required` actually specifies. Coverage must
    come from one presented detail — composing two narrow grants into an
    authority neither conveyed is deliberately not supported.
    """
    return any(_covers_one(required, p) for p in presented)


def _covers_one(required: AuthorizationDetail, presented: AuthorizationDetail) -> bool:
    if presented.type != required.type:
        return False
    if required.actions and not set(required.actions) <= set(presented.actions):
        return False
    if required.locations and not set(required.locations) <= set(presented.locations):
        return False
    if required.datatypes and not set(required.datatypes) <= set(presented.datatypes):
        return False
    if required.privileges and not set(required.privileges) <= set(presented.privileges):
        return False
    return not (required.identifier is not None and presented.identifier != required.identifier)


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RarError(f"authorization detail field {field_name!r} must be a list of strings")
    return tuple(value)


def parse_authorization_details(raw: object) -> tuple[AuthorizationDetail, ...]:
    """Parse a raw `authorization_details` value into domain objects.

    Presented details (unlike `require`) may carry type-specific extension
    fields per RFC 9396; only the fields sidekit's coverage predicate reads
    are extracted, and unrecognized fields are ignored rather than rejected —
    strict rejection is reserved for `require`, the one place policy must fail
    closed (§5).
    """
    if not isinstance(raw, list):
        raise RarError(f"authorization_details must be a list, got {type(raw).__name__}")

    parsed: list[AuthorizationDetail] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise RarError(f"authorization detail entries must be objects, got {type(entry).__name__}")
        detail_type = entry.get("type")
        if not isinstance(detail_type, str):
            raise RarError("authorization detail is missing a string 'type' field")
        identifier = entry.get("identifier")
        if identifier is not None and not isinstance(identifier, str):
            raise RarError("authorization detail field 'identifier' must be a string")
        parsed.append(
            AuthorizationDetail(
                type=detail_type,
                actions=_string_tuple(entry.get("actions", []), "actions"),
                locations=_string_tuple(entry.get("locations", []), "locations"),
                datatypes=_string_tuple(entry.get("datatypes", []), "datatypes"),
                identifier=identifier,
                privileges=_string_tuple(entry.get("privileges", []), "privileges"),
            )
        )
    return tuple(parsed)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth_rar.py -v`
Expected: 19 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/auth/rar.py tests/test_auth_rar.py
git commit -m "feat: RAR coverage predicate and authorization_details parsing"
```

---

### Task 2: RAR policy file models (`config/policy.py`)

**Files:**
- Create: `src/mcp_portal/config/policy.py`
- Create: `tests/test_config_policy.py`

**Interfaces:**
- Consumes: `Base`, `Effect`, `Sensitivity` re-exported from
  `mcp_portal.config.models`/`mcp_portal.operations` (existing).
- Produces: `AuthorizationDetailRequirement`, `RequireConfig`,
  `PolicyOutboundConfig`, `PolicyMatchSpec`, `PolicyRule`, `PolicyDefaults`,
  `PolicyConfig`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_policy.py`:

```python
import pytest
from pydantic import ValidationError

from mcp_portal.config.policy import PolicyConfig

MINIMAL: dict = {"version": "1"}


def test_minimal_policy_defaults_to_allow_and_no_rules():
    cfg = PolicyConfig.model_validate(MINIMAL)
    assert cfg.defaults.unmatched == "allow"
    assert cfg.rules == []


def test_unmatched_may_be_set_to_deny():
    cfg = PolicyConfig.model_validate(MINIMAL | {"defaults": {"unmatched": "deny"}})
    assert cfg.defaults.unmatched == "deny"


def test_unmatched_rejects_anything_outside_allow_or_deny():
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(MINIMAL | {"defaults": {"unmatched": "warn"}})


def _rule(**overrides) -> dict:
    rule = {
        "match": {"tags": ["billing"], "effect": ["action"]},
        "require": {
            "authorization_details": [
                {
                    "type": "payment_initiation",
                    "actions": ["initiate"],
                    "locations": ["https://api.example.com/v1/payments"],
                }
            ]
        },
    }
    return rule | overrides


def test_a_well_formed_rule_validates():
    cfg = PolicyConfig.model_validate(MINIMAL | {"rules": [_rule()]})
    rule = cfg.rules[0]
    assert rule.match.tags == ["billing"]
    assert rule.require.authorization_details[0].type == "payment_initiation"
    assert rule.outbound.carry is False


def test_outbound_carry_defaults_to_false_and_can_be_set():
    cfg = PolicyConfig.model_validate(
        MINIMAL | {"rules": [_rule(outbound={"carry": True})]}
    )
    assert cfg.rules[0].outbound.carry is True


def test_match_accepts_ids_tags_upstream_effect_and_sensitivity():
    rule = _rule(
        match={
            "ids": ["get_user_ssn", "export_*"],
            "tags": ["admin"],
            "upstream": ["billing"],
            "effect": ["action"],
            "sensitivity": ["sensitive"],
        }
    )
    cfg = PolicyConfig.model_validate(MINIMAL | {"rules": [rule]})
    assert cfg.rules[0].match.sensitivity == ["sensitive"]


def test_match_has_no_name_field():
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(
            MINIMAL | {"rules": [_rule(match={"name": ["list_invoices"]})]}
        )


def test_require_rejects_an_unknown_field():
    rule = _rule(
        require={
            "authorization_details": [
                {"type": "payment_initiation", "privileges": ["admin"], "bogus_field": True}
            ]
        }
    )
    with pytest.raises(ValidationError) as exc:
        PolicyConfig.model_validate(MINIMAL | {"rules": [rule]})
    assert "bogus_field" in str(exc.value)


@pytest.mark.parametrize("field_name", ["actions", "locations", "datatypes", "privileges"])
def test_require_rejects_an_empty_array_field(field_name):
    rule = _rule(
        require={"authorization_details": [{"type": "payment_initiation", field_name: []}]}
    )
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(MINIMAL | {"rules": [rule]})


def test_require_rejects_an_empty_authorization_details_list():
    rule = _rule(require={"authorization_details": []})
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate(MINIMAL | {"rules": [rule]})


def test_a_rule_may_omit_require_entirely_a_carry_only_rule():
    cfg = PolicyConfig.model_validate(
        MINIMAL | {"rules": [{"match": {"tags": ["billing"]}, "outbound": {"carry": True}}]}
    )
    assert cfg.rules[0].require is None
    assert cfg.rules[0].outbound.carry is True


def test_a_require_less_rule_is_fine_under_the_default_unmatched_allow():
    PolicyConfig.model_validate(
        MINIMAL | {"rules": [{"match": {"tags": ["billing"]}}]}
    )


def test_a_require_less_rule_is_a_load_error_under_unmatched_deny():
    payload = MINIMAL | {
        "defaults": {"unmatched": "deny"},
        "rules": [{"match": {"tags": ["billing"]}}],
    }
    with pytest.raises(ValidationError) as exc:
        PolicyConfig.model_validate(payload)
    assert "require" in str(exc.value)


def test_a_rule_with_require_is_fine_under_unmatched_deny():
    payload = MINIMAL | {"defaults": {"unmatched": "deny"}, "rules": [_rule()]}
    PolicyConfig.model_validate(payload)


def test_version_must_be_the_string_one():
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate({"version": "2"})
    with pytest.raises(ValidationError):
        PolicyConfig.model_validate({})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.config.policy'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/config/policy.py`:

```python
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
    def _non_empty(cls, value: list[AuthorizationDetailRequirement]) -> list[AuthorizationDetailRequirement]:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config_policy.py -v`
Expected: 16 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/config/policy.py tests/test_config_policy.py
git commit -m "feat: RAR policy file models with strict require parsing"
```

---

### Task 3: Main config gains `auth.local_principal` and `policy.file`

**Files:**
- Modify: `src/mcp_portal/config/models.py`
- Modify: `tests/test_config_models.py`
- Modify: `schema/config-v1.schema.json` (regenerated, not hand-edited)

**Interfaces:**
- Consumes: nothing new from other P3 modules (kept independent of
  `config/policy.py` — the main config only names *where* the policy file
  is, never its content).
- Produces: `LocalPrincipalConfig`, `AuthConfig`, `PolicyFileConfig`,
  `Config.auth: AuthConfig`, `Config.policy: PolicyFileConfig | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_models.py`:

```python
def test_auth_and_policy_are_both_optional():
    cfg = Config.model_validate(MINIMAL)
    assert cfg.auth.local_principal.authorization_details == []
    assert cfg.policy is None


def test_local_principal_accepts_a_list_of_raw_authorization_details():
    payload = MINIMAL | {
        "auth": {
            "local_principal": {
                "authorization_details": [
                    {"type": "payment_initiation", "actions": ["initiate"]}
                ]
            }
        }
    }
    cfg = Config.model_validate(payload)
    assert cfg.auth.local_principal.authorization_details[0]["type"] == "payment_initiation"


def test_policy_file_is_a_bare_path_string():
    cfg = Config.model_validate(MINIMAL | {"policy": {"file": "./rar-policy.yaml"}})
    assert cfg.policy.file == "./rar-policy.yaml"


def test_auth_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        Config.model_validate(MINIMAL | {"auth": {"inbound": {"enabled": True}}})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config_models.py -v`
Expected: the four new tests FAIL with `AttributeError` (`Config` has no
`auth`/`policy` attribute yet).

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/config/models.py`, add after `NamingConfig`:

```python
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
```

Modify `Config` to add two fields (after `naming`):

```python
    auth: AuthConfig = Field(default_factory=AuthConfig)
    policy: PolicyFileConfig | None = None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config_models.py -v`
Expected: all passed.

- [ ] **Step 5: Regenerate the schema and verify the drift check**

```bash
uv run python -m mcp_portal.config.schema
uv run pytest tests/test_schema_drift.py -v
```
Expected: schema rewritten; passed.

- [ ] **Step 6: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 7: Commit**

```bash
git add src/mcp_portal/config/models.py tests/test_config_models.py schema/config-v1.schema.json
git commit -m "feat: add auth.local_principal and policy.file to the main config"
```

---

### Task 4: Load and resolve the policy file (`config/loader.py`)

**Files:**
- Modify: `src/mcp_portal/config/loader.py`
- Modify: `tests/test_config_loader.py`

**Interfaces:**
- Consumes: `PolicyConfig` (Task 2); `Config.policy: PolicyFileConfig | None`
  (Task 3); the existing `_read` helper (JSON/YAML dispatch by suffix).
- Produces: `load_policy_file(policy: PolicyFileConfig, base_dir: Path) -> PolicyConfig`;
  `LoadedConfig.policy: PolicyConfig | None`; `load_config` now resolves it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_loader.py`:

```python
from pydantic import ValidationError

from mcp_portal.config.loader import load_policy_file
from mcp_portal.config.models import PolicyFileConfig
from mcp_portal.config.policy import PolicyConfig


def test_load_config_with_no_policy_field_has_no_policy(tmp_path: Path):
    loaded = load_config(write(tmp_path, MINIMAL))
    assert loaded.policy is None


def test_load_config_resolves_a_policy_file_relative_to_the_config_directory(tmp_path: Path):
    (tmp_path / "rar-policy.yaml").write_text("version: '1'\ndefaults: {unmatched: deny}\n")
    loaded = load_config(write(tmp_path, MINIMAL | {"policy": {"file": "./rar-policy.yaml"}}))
    assert loaded.policy is not None
    assert loaded.policy.defaults.unmatched == "deny"


def test_load_config_resolves_a_policy_file_relative_to_the_config_directory_even_when_launched_elsewhere(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "rar-policy.yaml").write_text("version: '1'\n")
    monkeypatch.chdir(tmp_path.parent)
    loaded = load_config(write(tmp_path, MINIMAL | {"policy": {"file": "rar-policy.yaml"}}))
    assert loaded.policy is not None


def test_load_config_raises_a_config_error_for_a_missing_policy_file(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, MINIMAL | {"policy": {"file": "./nope.yaml"}}))


def test_load_config_raises_a_config_error_for_an_invalid_policy_file(tmp_path: Path):
    (tmp_path / "rar-policy.yaml").write_text("version: '1'\ndefaults: {unmatched: bogus}\n")
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, MINIMAL | {"policy": {"file": "./rar-policy.yaml"}}))


def test_load_policy_file_reads_json(tmp_path: Path):
    (tmp_path / "policy.json").write_text('{"version": "1"}')
    policy = load_policy_file(PolicyFileConfig(file="policy.json"), tmp_path)
    assert isinstance(policy, PolicyConfig)


def test_load_policy_file_reads_yaml(tmp_path: Path):
    (tmp_path / "policy.yaml").write_text("version: '1'\n")
    policy = load_policy_file(PolicyFileConfig(file="policy.yaml"), tmp_path)
    assert policy.version == "1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config_loader.py -v`
Expected: FAIL — `ImportError` on `load_policy_file`, and `AttributeError`
on `loaded.policy`.

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/config/loader.py`, add the import and extend
`LoadedConfig` and `load_config`:

```python
from pydantic import ValidationError

from mcp_portal.config.models import SECRET_REF_PATTERN, Config, PolicyFileConfig
from mcp_portal.config.policy import PolicyConfig
```

```python
@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: Config
    base_dir: Path
    secrets: dict[str, str]
    policy: PolicyConfig | None = None
```

```python
def load_policy_file(policy: PolicyFileConfig, base_dir: Path) -> PolicyConfig:
    """Load and validate the RAR policy file, resolved relative to `base_dir` —
    the directory containing the *main* config file, per §5, so a config
    behaves identically regardless of where the process is launched from."""
    path = Path(policy.file)
    resolved = path if path.is_absolute() else base_dir / path
    raw = _read(resolved)
    try:
        return PolicyConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid policy file {resolved}:\n{exc}") from exc
```

`_read` already raises `ConfigError` for an unreadable path or invalid JSON,
so the "unresolvable `policy.file`" startup-validation case (§10, item 10)
is covered without new code there.

Modify `load_config`'s return to resolve the policy file:

```python
    check_header_denylist(config)

    secrets: dict[str, str] = {}
    _collect_secrets(raw, base_dir, secrets)

    policy = load_policy_file(config.policy, base_dir) if config.policy is not None else None

    return LoadedConfig(config=config, base_dir=base_dir, secrets=secrets, policy=policy)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config_loader.py -v`
Expected: all passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/config/loader.py tests/test_config_loader.py
git commit -m "feat: load and resolve the RAR policy file relative to the config directory"
```

---

### Task 5: The local principal (`auth/principal.py`)

**Files:**
- Create: `src/mcp_portal/auth/principal.py`
- Create: `tests/test_auth_principal.py`

**Interfaces:**
- Consumes: `AuthorizationDetail`, `RarError`, `parse_authorization_details`
  (Task 1); `LocalPrincipalConfig` (Task 3); `ConfigError` (existing,
  `config/loader.py`).
- Produces: `Principal` (frozen dataclass: `identity: str`,
  `authorization_details: tuple[AuthorizationDetail, ...]`),
  `local_principal(config: LocalPrincipalConfig) -> Principal`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth_principal.py`:

```python
import pytest

from mcp_portal.auth.principal import Principal, local_principal
from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import LocalPrincipalConfig


def test_default_local_principal_has_no_authorization_details():
    principal = local_principal(LocalPrincipalConfig())
    assert isinstance(principal, Principal)
    assert principal.identity == "local"
    assert principal.authorization_details == ()


def test_local_principal_parses_configured_authorization_details():
    config = LocalPrincipalConfig(
        authorization_details=[
            {"type": "payment_initiation", "actions": ["initiate"], "identifier": "acct-1"}
        ]
    )
    principal = local_principal(config)
    (detail,) = principal.authorization_details
    assert detail.type == "payment_initiation"
    assert detail.actions == ("initiate",)
    assert detail.identifier == "acct-1"


def test_a_malformed_authorization_detail_is_a_startup_config_error():
    # Missing 'type' — parse_authorization_details raises RarError, which a
    # config-sourced principal must surface as ConfigError: every
    # configuration problem is a load error, never a call-time surprise (§10).
    config = LocalPrincipalConfig(authorization_details=[{"actions": ["initiate"]}])
    with pytest.raises(ConfigError) as exc:
        local_principal(config)
    assert "type" in str(exc.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_auth_principal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.auth.principal'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/auth/principal.py`:

```python
"""The calling identity a policy decision is evaluated against.

Per §8 of the design, `transport: stdio` always uses the locally-asserted
principal built here. `transport: http` (P4) additionally derives one from a
validated bearer token, or falls back to this same local principal when
inbound auth is disabled — this module does not need to know which case
applies, since `Principal` is transport-agnostic.
"""

from dataclasses import dataclass

from mcp_portal.auth.rar import AuthorizationDetail, RarError, parse_authorization_details
from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import LocalPrincipalConfig


@dataclass(frozen=True, slots=True)
class Principal:
    identity: str
    authorization_details: tuple[AuthorizationDetail, ...]


def local_principal(config: LocalPrincipalConfig) -> Principal:
    """Build the self-asserted principal from `auth.local_principal` config.

    What this guardrail is honestly for: the threat model under stdio is not
    a malicious operator, it is an over-eager agent. Constraining which
    tools a model may invoke is worthwhile even though the human launching
    the process can trivially bypass it by editing the config directly — that
    is a guardrail, not a security boundary.
    """
    try:
        details = parse_authorization_details(config.authorization_details)
    except RarError as exc:
        raise ConfigError(f"auth.local_principal.authorization_details: {exc}") from exc
    return Principal(identity="local", authorization_details=details)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth_principal.py -v`
Expected: 3 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/auth/principal.py tests/test_auth_principal.py
git commit -m "feat: build the locally-asserted principal from config"
```

---

### Task 6: The policy engine (`policy.py`)

**Files:**
- Create: `src/mcp_portal/policy.py`
- Create: `tests/test_policy.py`

**Interfaces:**
- Consumes: `PolicyConfig`, `PolicyRule`, `PolicyMatchSpec` (Task 2);
  `AuthorizationDetail`, `covers` (Task 1); `Principal` (Task 5); `Operation`,
  `Effect`, `Sensitivity` (existing, `operations.py`); `fnmatch` (stdlib, same
  matching primitive `registry.py` already uses).
- Produces: `PolicyDecision` (frozen dataclass: `allowed: bool`,
  `missing: tuple[AuthorizationDetail, ...] = ()`, `carry: bool = False`),
  `PolicyEngine(policy: PolicyConfig)` with `.evaluate(operation, principal)
  -> PolicyDecision` and `.dead_rule_warnings(operations: Sequence[Operation])
  -> list[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_policy.py`:

```python
from mcp_portal.auth.principal import Principal
from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.config.policy import PolicyConfig
from mcp_portal.operations import (
    Effect,
    HttpBinding,
    Operation,
    Sensitivity,
)
from mcp_portal.policy import PolicyEngine


def op(
    op_id: str = "initiate_payment",
    effect: Effect = Effect.ACTION,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
    tags: tuple[str, ...] = ("billing",),
    upstream: str = "billing",
) -> Operation:
    return Operation(
        id=op_id,
        upstream=upstream,
        name=op_id,
        title=op_id,
        description="d",
        group_tags=tags,
        effect=effect,
        sensitivity=sensitivity,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method="POST", path="/x"),
    )


def principal(*details: AuthorizationDetail) -> Principal:
    return Principal(identity="local", authorization_details=details)


PAYMENT_RULE: dict = {
    "match": {"tags": ["billing"], "effect": ["action"]},
    "require": {
        "authorization_details": [
            {
                "type": "payment_initiation",
                "actions": ["initiate"],
                "locations": ["https://api.example.com/v1/payments"],
            }
        ]
    },
}


def test_default_policy_allows_everything():
    engine = PolicyEngine(PolicyConfig(version="1"))
    decision = engine.evaluate(op(), principal())
    assert decision.allowed is True
    assert decision.missing == ()


def test_a_non_matching_operation_falls_back_to_unmatched_default_allow():
    engine = PolicyEngine(PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]}))
    decision = engine.evaluate(op(tags=("shipping",)), principal())
    assert decision.allowed is True


def test_a_non_matching_operation_falls_back_to_unmatched_default_deny():
    cfg = PolicyConfig.model_validate(
        {"version": "1", "defaults": {"unmatched": "deny"}, "rules": [PAYMENT_RULE]}
    )
    decision = PolicyEngine(cfg).evaluate(op(tags=("shipping",)), principal())
    assert decision.allowed is False


def test_a_matched_operation_with_covering_details_is_allowed():
    cfg = PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]})
    presented = AuthorizationDetail(
        type="payment_initiation",
        actions=("initiate", "cancel"),
        locations=("https://api.example.com/v1/payments",),
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal(presented))
    assert decision.allowed is True
    assert decision.missing == ()


def test_a_matched_operation_with_no_presented_details_is_denied():
    cfg = PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]})
    decision = PolicyEngine(cfg).evaluate(op(), principal())
    assert decision.allowed is False
    assert decision.missing[0].type == "payment_initiation"


def test_a_matched_operation_with_a_non_covering_detail_is_denied():
    cfg = PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]})
    wrong = AuthorizationDetail(type="payment_initiation", actions=("cancel",))
    decision = PolicyEngine(cfg).evaluate(op(), principal(wrong))
    assert decision.allowed is False


def test_requirements_accumulate_across_every_matching_rule():
    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                PAYMENT_RULE,
                {
                    "match": {"tags": ["billing"]},
                    "require": {"authorization_details": [{"type": "audit_log", "actions": ["write"]}]},
                },
            ],
        }
    )
    presented_payment = AuthorizationDetail(
        type="payment_initiation",
        actions=("initiate",),
        locations=("https://api.example.com/v1/payments",),
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal(presented_payment))
    assert decision.allowed is False
    assert {d.type for d in decision.missing} == {"audit_log"}

    presented_both = (presented_payment, AuthorizationDetail(type="audit_log", actions=("write",)))
    decision = PolicyEngine(cfg).evaluate(op(), principal(*presented_both))
    assert decision.allowed is True


def test_a_carry_only_rule_does_not_count_as_matched_under_unmatched_deny():
    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "defaults": {"unmatched": "deny"},
            "rules": [{"match": {"tags": ["billing"]}, "outbound": {"carry": True}}],
        }
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal())
    assert decision.allowed is False


def test_carry_is_true_when_any_matching_rule_sets_it():
    cfg = PolicyConfig.model_validate(
        {"version": "1", "rules": [{"match": {"tags": ["billing"]}, "outbound": {"carry": True}}]}
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal())
    assert decision.allowed is True
    assert decision.carry is True


def test_carry_is_false_when_no_matching_rule_sets_it():
    cfg = PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]})
    presented = AuthorizationDetail(
        type="payment_initiation",
        actions=("initiate",),
        locations=("https://api.example.com/v1/payments",),
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal(presented))
    assert decision.carry is False


def test_match_on_sensitivity():
    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "defaults": {"unmatched": "deny"},
            "rules": [
                {
                    "match": {"sensitivity": ["sensitive"]},
                    "require": {"authorization_details": [{"type": "x"}]},
                }
            ],
        }
    )
    engine = PolicyEngine(cfg)
    assert engine.evaluate(op(sensitivity=Sensitivity.NORMAL), principal()).allowed is True
    assert engine.evaluate(op(sensitivity=Sensitivity.SENSITIVE), principal()).allowed is False


def test_match_on_ids_and_upstream():
    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "defaults": {"unmatched": "deny"},
            "rules": [
                {
                    "match": {"ids": ["export_*"], "upstream": ["billing"]},
                    "require": {"authorization_details": [{"type": "x"}]},
                }
            ],
        }
    )
    engine = PolicyEngine(cfg)
    assert engine.evaluate(op(op_id="export_report"), principal()).allowed is False
    assert engine.evaluate(op(op_id="list_invoices"), principal()).allowed is True


def test_dead_rule_warnings_names_a_rule_matching_nothing():
    cfg = PolicyConfig.model_validate(
        {"version": "1", "rules": [{"match": {"tags": ["nonexistent"]}, "outbound": {"carry": True}}]}
    )
    warnings = PolicyEngine(cfg).dead_rule_warnings([op()])
    assert len(warnings) == 1
    assert "nonexistent" in warnings[0]


def test_dead_rule_warnings_is_empty_when_every_rule_matches_something():
    cfg = PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]})
    assert PolicyEngine(cfg).dead_rule_warnings([op()]) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.policy'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/policy.py`:

```python
"""Rule matching, requirement accumulation, and the allow/deny decision.

Matching reuses the same vocabulary `registry.py` uses for selection — `id`
globs, `tags`, `upstream`, `effect` — extended with `sensitivity`, which
policy alone needs (registry's own `MatchSpec` intentionally does not carry
it: sensitivity is consumed at registry-build time and settable there, not
here). There is deliberately no `match.name`, for the reason `registry.py`
already gives: names are unstable by construction.
"""

import dataclasses
from collections.abc import Sequence
from fnmatch import fnmatch

from mcp_portal.auth.principal import Principal
from mcp_portal.auth.rar import AuthorizationDetail, covers
from mcp_portal.config.policy import PolicyConfig, PolicyMatchSpec, PolicyRule
from mcp_portal.operations import Operation


@dataclasses.dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    missing: tuple[AuthorizationDetail, ...] = ()
    carry: bool = False


def _matches(op: Operation, spec: PolicyMatchSpec) -> bool:
    if spec.ids and not any(fnmatch(op.id, pattern) for pattern in spec.ids):
        return False
    if spec.tags and not set(spec.tags) & set(op.group_tags):
        return False
    if spec.upstream and op.upstream not in spec.upstream:
        return False
    if spec.effect and op.effect not in spec.effect:
        return False
    return not (spec.sensitivity and op.sensitivity not in spec.sensitivity)


def _matching_rules(op: Operation, rules: Sequence[PolicyRule]) -> list[PolicyRule]:
    return [rule for rule in rules if _matches(op, rule.match)]


class PolicyEngine:
    def __init__(self, policy: PolicyConfig) -> None:
        self._policy = policy

    def evaluate(self, operation: Operation, principal: Principal) -> PolicyDecision:
        matching = _matching_rules(operation, self._policy.rules)
        carry = any(rule.outbound.carry for rule in matching)

        # "Matched" means "contributed a requirement" (§5) — a carry-only
        # rule's match still sets `carry` above, but never makes an operation
        # matched for the purposes of `defaults.unmatched`.
        required: list[AuthorizationDetail] = []
        for rule in matching:
            if rule.require is None:
                continue
            required.extend(
                AuthorizationDetail(
                    type=d.type,
                    actions=tuple(d.actions or ()),
                    locations=tuple(d.locations or ()),
                    datatypes=tuple(d.datatypes or ()),
                    identifier=d.identifier,
                    privileges=tuple(d.privileges or ()),
                )
                for d in rule.require.authorization_details
            )

        if not required:
            allowed = self._policy.defaults.unmatched == "allow"
            return PolicyDecision(allowed=allowed, carry=carry)

        missing = tuple(r for r in required if not covers(r, principal.authorization_details))
        return PolicyDecision(allowed=not missing, missing=missing, carry=carry)

    def dead_rule_warnings(self, operations: Sequence[Operation]) -> list[str]:
        warnings: list[str] = []
        for index, rule in enumerate(self._policy.rules):
            if not any(_matches(op, rule.match) for op in operations):
                warnings.append(
                    f"policy rule at index {index} (match={rule.match!r}) matched no "
                    "operations; it may be stale"
                )
        return warnings
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_policy.py -v`
Expected: 15 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/policy.py tests/test_policy.py
git commit -m "feat: policy rule matching, requirement accumulation, and decision"
```

---

### Task 7: Enforce policy in `ToolInvoker.call`

**Files:**
- Modify: `src/mcp_portal/server/mcp.py`
- Modify: `tests/test_server_mcp.py`

**Interfaces:**
- Consumes: `PolicyEngine`, `PolicyDecision` (Task 6); `Principal` (Task 5).
- Produces: `ToolInvoker.__init__` gains `policy: PolicyEngine | None = None`
  and `principal: Principal | None = None`, both optional so every existing
  P1/P2 call site keeps working unchanged and unenforced.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_mcp.py`:

```python
from mcp_portal.auth.principal import Principal
from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.config.policy import PolicyConfig
from mcp_portal.policy import PolicyEngine


def invoker_with_policy(handler, operation: Operation, policy: PolicyEngine, principal: Principal) -> ToolInvoker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = UpstreamConfig(base_url="https://api.example.com")
    toolset = ToolSet(operations=(operation,), by_name={operation.name: operation})
    return ToolInvoker(
        toolset=toolset,
        transports={"billing": HttpTransport(client, upstream, None)},
        policy=policy,
        principal=principal,
    )


@pytest.mark.anyio
async def test_a_call_with_no_policy_engine_is_unaffected_p1_p2_behavior():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    result = await invoker(handler, op()).call("list_invoices", {})
    assert result.is_error is False


@pytest.mark.anyio
async def test_a_denied_call_never_reaches_the_transport():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                {
                    "match": {"effect": ["action"]},
                    "require": {"authorization_details": [{"type": "payment_initiation"}]},
                }
            ],
        }
    )
    result = await invoker_with_policy(
        handler, op(effect=Effect.ACTION), PolicyEngine(cfg), Principal("local", ())
    ).call("list_invoices", {})

    assert result.is_error is True
    assert "payment_initiation" in result.content[0].text
    assert calls == []


@pytest.mark.anyio
async def test_an_allowed_call_proceeds_to_the_transport():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                {
                    "match": {"effect": ["action"]},
                    "require": {"authorization_details": [{"type": "payment_initiation"}]},
                }
            ],
        }
    )
    principal = Principal("local", (AuthorizationDetail(type="payment_initiation"),))
    result = await invoker_with_policy(
        handler, op(effect=Effect.ACTION), PolicyEngine(cfg), principal
    ).call("list_invoices", {})

    assert result.is_error is False


@pytest.mark.anyio
async def test_invalid_arguments_are_rejected_before_policy_is_even_consulted():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    operation = op()
    schema_op = dataclasses.replace(
        operation, input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    )
    cfg = PolicyConfig.model_validate(
        {"version": "1", "defaults": {"unmatched": "deny"}, "rules": []}
    )
    result = await invoker_with_policy(
        handler, schema_op, PolicyEngine(cfg), Principal("local", ())
    ).call("list_invoices", {})

    assert result.is_error is True
    assert "invalid arguments" in result.content[0].text
```

Add `import dataclasses` to the top of `tests/test_server_mcp.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_server_mcp.py -v`
Expected: FAIL — `TypeError: ToolInvoker.__init__() got an unexpected keyword
argument 'policy'`.

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/server/mcp.py`, add imports and change `ToolInvoker`:

```python
from mcp_portal.auth.principal import Principal
from mcp_portal.policy import PolicyEngine
```

```python
class ToolInvoker:
    def __init__(
        self,
        toolset: ToolSet,
        transports: Mapping[str, HttpTransport],
        policy: PolicyEngine | None = None,
        principal: Principal | None = None,
    ) -> None:
        self._toolset = toolset
        self._transports = transports
        self._policy = policy
        self._principal = principal if principal is not None else Principal("local", ())

    def tools(self) -> list[types.Tool]:
        return [to_mcp_tool(op) for op in self._toolset.operations]

    async def call(self, name: str, arguments: Mapping[str, Any] | None) -> types.CallToolResult:
        operation = self._toolset.by_name.get(name)
        if operation is None:
            return _error(f"unknown tool {name!r}")

        args = dict(arguments or {})
        try:
            jsonschema.validate(args, dict(operation.input_schema))
        except jsonschema.ValidationError as exc:
            field = ".".join(str(p) for p in exc.absolute_path)
            where = f" at {field}" if field else ""
            return _error(f"invalid arguments for {name!r}{where}: {exc.message}")

        if self._policy is not None:
            decision = self._policy.evaluate(operation, self._principal)
            if not decision.allowed:
                missing = ", ".join(d.type for d in decision.missing) or "policy default is deny"
                # Never the token/principal contents (§10) — only which
                # requirement type was missing.
                return _error(f"authorization denied for {name!r}: missing {missing}")

        transport = self._transports.get(operation.upstream)
        if transport is None:
            return _error(f"no transport configured for upstream {operation.upstream!r}")

        try:
            response = await transport.execute(operation, args)
        except RequestBuildError as exc:
            return _error(f"invalid arguments for {name!r}: {exc}")
        except httpx.TimeoutException:
            return _error(f"upstream {operation.upstream!r} timed out")
        except httpx.RequestError as exc:
            return _error(f"upstream {operation.upstream!r} request failed: {exc}")

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=response.text)],
            is_error=not (200 <= response.status < 300),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_server_mcp.py -v`
Expected: all passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/server/mcp.py tests/test_server_mcp.py
git commit -m "feat: enforce RAR policy between argument validation and transport execution"
```

---

### Task 8: Wire policy and the local principal into `build_app`

**Files:**
- Modify: `src/mcp_portal/app.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `PolicyConfig` (Task 2, via `LoadedConfig.policy`); `PolicyEngine`
  (Task 6); `local_principal` (Task 5).
- Produces: `build_app` now constructs and injects a `PolicyEngine` and
  `Principal` into `ToolInvoker`, logs dead-rule warnings alongside the
  existing selection ones, and logs the local-principal guardrail banner.

- [ ] **Step 1: Write the failing tests**

Inspect `tests/test_app.py` first to match its existing fixture style
(`uv run pytest tests/test_app.py --collect-only -q` to see current test
names), then append:

```python
import logging

from mcp_portal.auth.principal import Principal
from mcp_portal.policy import PolicyEngine


def test_build_app_with_no_policy_file_allows_every_call(tmp_path, respx_mock=None):
    # Reuses this file's existing `write_config`/`minimal_config`-style helper
    # (see the surrounding fixtures) with no `policy` key — behavior must be
    # identical to P1/P2.
    loaded = load_config(write_config(tmp_path, MINIMAL_CONFIG))
    app = build_app(loaded)
    assert app.invoker._policy is not None
    assert app.invoker._policy.evaluate(app.invoker._toolset.operations[0], Principal("local", ())).allowed


def test_build_app_wires_the_configured_local_principal(tmp_path):
    payload = MINIMAL_CONFIG | {
        "auth": {
            "local_principal": {
                "authorization_details": [{"type": "payment_initiation", "actions": ["initiate"]}]
            }
        }
    }
    loaded = load_config(write_config(tmp_path, payload))
    app = build_app(loaded)
    assert app.invoker._principal.authorization_details[0].type == "payment_initiation"


def test_build_app_wires_a_policy_file_and_enforces_it(tmp_path):
    (tmp_path / "rar-policy.yaml").write_text(
        "version: '1'\n"
        "defaults: {unmatched: deny}\n"
        "rules:\n"
        "  - match: {upstream: [billing]}\n"
        "    require: {authorization_details: [{type: payment_initiation}]}\n"
    )
    payload = MINIMAL_CONFIG | {"policy": {"file": "./rar-policy.yaml"}}
    loaded = load_config(write_config(tmp_path, payload))
    app = build_app(loaded)
    operation = app.invoker._toolset.operations[0]
    assert app.invoker._policy.evaluate(operation, Principal("local", ())).allowed is False


def test_build_app_logs_a_dead_policy_rule_warning(tmp_path, caplog):
    (tmp_path / "rar-policy.yaml").write_text(
        "version: '1'\n"
        "rules:\n"
        "  - match: {tags: [nonexistent]}\n"
        "    require: {authorization_details: [{type: x}]}\n"
    )
    payload = MINIMAL_CONFIG | {"policy": {"file": "./rar-policy.yaml"}}
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        build_app(load_config(write_config(tmp_path, payload)))
    assert "nonexistent" in caplog.text
```

Adjust the exact helper names (`write_config`, `MINIMAL_CONFIG`) to whatever
`tests/test_app.py` already defines — do not introduce a second, differently
named fixture for the same minimal config.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_app.py -v`
Expected: FAIL — `AttributeError: 'ToolInvoker' object has no attribute
'_policy'` is `None` where a real engine was expected, or the dead-rule
warning is absent.

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/app.py`, add imports:

```python
from mcp_portal.auth.principal import local_principal
from mcp_portal.config.policy import PolicyConfig, PolicyDefaults
from mcp_portal.policy import PolicyEngine
```

In `build_app`, after `toolset` is built and its own warnings are logged,
insert policy construction before the transports loop:

```python
    for warning in toolset.warnings:
        log.warning("%s", warning)

    policy_config = loaded.policy or PolicyConfig(version="1", defaults=PolicyDefaults())
    policy = PolicyEngine(policy_config)
    for warning in policy.dead_rule_warnings(toolset.operations):
        log.warning("%s", warning)

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
```

Modify the `ToolInvoker` construction to pass both through:

```python
    return App(
        invoker=ToolInvoker(toolset, transports, policy=policy, principal=principal),
        warnings=toolset.warnings,
        _clients=clients,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_app.py -v`
Expected: all passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/app.py tests/test_app.py
git commit -m "feat: wire the RAR policy engine and local principal into build_app"
```

---

### Task 9: End-to-end fixture and full P3 suite

**Files:**
- Create: `tests/test_p3_end_to_end.py`
- Create: `tests/fixtures/p3-config.yaml`
- Create: `tests/fixtures/p3-policy.yaml`

**Interfaces:**
- Consumes: everything from Tasks 1–8. No production code changes in this
  task — it is the integration check that the pieces actually compose the
  way §5's worked example (billing / `payment_initiation`) describes.

- [ ] **Step 1: Write the fixture files**

Create `tests/fixtures/p3-config.yaml`:

```yaml
version: "1"
mode: configured
server:
  name: billing-portal
  transport: stdio
upstreams:
  billing:
    base_url: https://api.example.com
operations:
  - id: initiate_payment
    upstream: billing
    description: Initiate a payment.
    group_tags: [billing]
    binding:
      protocol: http
      method: POST
      path: /v1/payments
      body:
        content_type: application/json
        schema: { type: object, properties: { amount: { type: integer } } }
  - id: list_invoices
    upstream: billing
    description: List invoices.
    group_tags: [billing]
    binding:
      protocol: http
      method: GET
      path: /v1/invoices
auth:
  local_principal:
    authorization_details:
      - type: payment_initiation
        actions: [initiate]
        locations: ["https://api.example.com/v1/payments"]
policy:
  file: ./p3-policy.yaml
```

Create `tests/fixtures/p3-policy.yaml`:

```yaml
version: "1"
defaults:
  unmatched: allow
rules:
  - match:
      tags: [billing]
      effect: [action]
    require:
      authorization_details:
        - type: payment_initiation
          actions: [initiate]
          locations: ["https://api.example.com/v1/payments"]
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_p3_end_to_end.py`:

```python
from pathlib import Path

import httpx
import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.anyio
async def test_the_action_operation_is_allowed_when_the_local_principal_covers_it():
    loaded = load_config(FIXTURES / "p3-config.yaml")
    app = build_app(loaded)
    for transport in app.invoker._transports.values():
        transport._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
        )
    result = await app.invoker.call("initiate_payment", {"amount": 100})
    assert result.is_error is False
    await app.aclose()


@pytest.mark.anyio
async def test_the_read_only_operation_is_unaffected_by_the_action_only_rule():
    loaded = load_config(FIXTURES / "p3-config.yaml")
    app = build_app(loaded)
    for transport in app.invoker._transports.values():
        transport._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        )
    result = await app.invoker.call("list_invoices", {})
    assert result.is_error is False
    await app.aclose()


@pytest.mark.anyio
async def test_removing_the_local_principals_authorization_detail_denies_the_action():
    raw_config = (FIXTURES / "p3-config.yaml").read_text().replace(
        "        actions: [initiate]\n        locations:"
        ' ["https://api.example.com/v1/payments"]\n',
        "",
    )
    config_path = FIXTURES / "p3-config-no-principal-detail.yaml"
    config_path.write_text(raw_config)
    try:
        loaded = load_config(config_path)
        app = build_app(loaded)
        result = await app.invoker.call("initiate_payment", {"amount": 100})
        assert result.is_error is True
        assert "payment_initiation" in result.content[0].text
        await app.aclose()
    finally:
        config_path.unlink()
```

Look at how `HttpTransport` stores its client in `src/mcp_portal/transports/http.py`
(the constructor argument name) before writing `transport._client = ...` above
— match the actual private attribute name rather than guessing.

- [ ] **Step 2: Run the test to verify the third case fails without policy wiring, then passes**

Run: `uv run pytest tests/test_p3_end_to_end.py -v`
Expected: first two pass immediately (they exercise `configured` mode logic
already covered by P1/P2); the third depends on Tasks 1–8 already being
implemented in this plan, so it should already pass at this point — this
task is a regression fixture, not new production code. If it fails, the bug
is in an earlier task, not this one; go fix that task.

- [ ] **Step 3: Run the entire test suite together**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
uv run pytest -v
```
Expected: every test in the project passes, including all of P1, P2, and P3.

- [ ] **Step 4: Regenerate the schema one last time and confirm no drift**

```bash
uv run python -m mcp_portal.config.schema
git diff --stat schema/config-v1.schema.json
```
Expected: no diff — Task 3 already committed the current shape.

- [ ] **Step 5: Commit**

```bash
git add tests/test_p3_end_to_end.py tests/fixtures/p3-config.yaml tests/fixtures/p3-policy.yaml
git commit -m "test: end-to-end RAR enforcement fixture covering the design's billing example"
```

---

## Notify: full suite run required

This plan's tasks each run only their own test file (per the Global
Constraints and existing P1/P2 convention). **Task 9, Step 3 is the only
point the full suite runs.** Before considering P3 done, run:

```bash
uv run pytest -v
```

and confirm nothing outside this plan's own files regressed.
