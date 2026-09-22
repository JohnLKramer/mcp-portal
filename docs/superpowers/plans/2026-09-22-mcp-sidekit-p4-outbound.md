# mcp-portal P4a (Outbound Broker) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `client_credentials` outbound auth mode (RFC 6749), a
single-flighted TTL token cache shared by every dynamic outbound mode, and the
`carry` plumbing that lets a policy rule attach the exact authority a call
needed to the credential the gateway presents upstream. Also build (but do not
yet wire into the runtime call path) the pure `token_exchange` (RFC 8693)
acquisition function, since it shares every piece of infrastructure this plan
builds and there is no reason to write the HTTP-POST-and-parse-response logic
twice.

**Architecture:** Per the design's own phasing note (§12), P4's inbound
(resource server, JWKS, discovery, Origin) and outbound (`client_credentials`,
`token_exchange`, cache) halves "share no code, only config." This plan is the
outbound half and is fully exercisable under `transport: stdio` today —
`client_credentials` needs no inbound token. `token_exchange` *does* need one
(the design calls it the `subject_token`), so this plan implements its
acquisition logic as a standalone, directly-testable function but does not add
`mode: "token_exchange"` to the config schema or wire it into `ToolInvoker`:
publishing that config value without a real subject token to feed it would be
exactly the "schema field that silently does nothing" the design's phasing
rule (§12: "a phase that publishes a config field must implement it")
forbids. The sibling plan
(`docs/superpowers/plans/2026-09-22-mcp-sidekit-p4-inbound.md`) adds the
config literal, the startup validation guarding it, and the real subject
token in the same task that makes it reachable.

**Tech Stack:** Same as P1–P3 (Python 3.14.7, uv, Pydantic v2, httpx, `mcp`
SDK, ruamel.yaml, jsonschema, pytest, ruff, mypy). No new dependencies —
`httpx.AsyncClient` already in use for upstream calls is reused for token
endpoint calls.

**Spec:** [`docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md`](../specs/2026-09-19-mcp-sidekit-design.md)

## Global Constraints

- **Scope is P4-outbound only.** Not in this plan: `auth/inbound.py`, JWKS,
  RFC 9728 discovery, `server/http.py`, `transport: http`, or
  `auth.inbound.*` config. Do not add those fields or modules here — they are
  the sibling P4-inbound plan.
- **`mode: "token_exchange"` is not added to `OutboundConfig`'s `Literal` in
  this plan.** The acquisition function is built and unit-tested directly;
  making it reachable from config is the sibling plan's job, alongside the
  startup validation that makes stdio/inbound-disabled + token_exchange a
  load error (§10, item 4). Landing the schema field and its enforcement in
  different plans would violate "a phase that publishes a config field must
  implement it."
- **`carry` sends the accumulated required detail `R`, never the presented
  detail or the caller's full `authorization_details`** (§8) — least
  privilege. This is why `PolicyDecision.carry` changes from a bool to the
  actual tuple of details in this plan: a bool told the P3 caller (which
  consumed nothing) whether to carry, but P4's outbound broker needs to know
  *what*.
- **Cache keys:** `client_credentials` → `upstream + scopes + hash(carried
  details)`, TTL = token `expires_in` minus a leeway (`_EXPIRY_LEEWAY_S = 60`).
  `client_credentials` deliberately excludes any subject/principal identity —
  that grant does not depend on the caller (§8).
- **Every cache key is single-flighted**: a burst of concurrent tool calls for
  the same key must trigger exactly one IdP request, never one per caller.
- **`client_secret` is a `SecretRef`** (`${env:...}`/`${file:...}`), never a
  literal — same rule every other secret-typed field in the config already
  follows.
- Every task ends with `uv run ruff format .`, `uv run ruff check .`,
  `uv run mypy src`, and the task's own test file passing before the commit
  step. Do not run the full suite mid-task; the final task is where
  everything runs together.

---

### Task 1: `OutboundConfig` gains the `client_credentials` mode

**Files:**
- Modify: `src/mcp_portal/config/models.py`
- Modify: `tests/test_config_models.py`
- Modify: `schema/config-v1.schema.json` (regenerated, not hand-edited)

**Interfaces:**
- Consumes: `Base`, `SecretRef`, `BaseUrl` (existing, same file).
- Produces: `OutboundConfig.mode` gains `"client_credentials"`;
  `OutboundConfig` gains `token_endpoint: BaseUrl | None`,
  `client_id: str | None`, `client_secret: SecretRef | None`,
  `scopes: list[str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_models.py`:

```python
def test_client_credentials_mode_requires_token_endpoint_client_id_and_secret():
    with pytest.raises(ValidationError):
        OutboundConfig(mode="client_credentials")


def test_client_credentials_mode_validates_with_all_three():
    cfg = OutboundConfig(
        mode="client_credentials",
        token_endpoint="https://idp.example.com/oauth2/token",
        client_id="sidekit-billing",
        client_secret="${env:BILLING_CLIENT_SECRET}",
        scopes=["invoices.write"],
    )
    assert cfg.token_endpoint == "https://idp.example.com/oauth2/token"
    assert cfg.scopes == ["invoices.write"]


def test_client_credentials_mode_rejects_a_literal_client_secret():
    with pytest.raises(ValidationError):
        OutboundConfig(
            mode="client_credentials",
            token_endpoint="https://idp.example.com/oauth2/token",
            client_id="sidekit-billing",
            client_secret="not-a-reference",
        )
```

(`OutboundConfig` and `ValidationError` are already imported at the top of
`tests/test_config_models.py`; add the import if not present.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config_models.py -v`
Expected: FAIL — `client_credentials` is not a valid `mode` today
(`ValidationError` is raised for the wrong reason, or the constructor accepts
it silently with no cross-field check).

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/config/models.py`, replace `OutboundConfig`:

```python
class OutboundConfig(Base):
    mode: Literal["none", "static", "client_credentials"] = "none"
    header: str = "Authorization"
    scheme: str | None = "Bearer"
    value: SecretRef | None = None
    token_endpoint: BaseUrl | None = None
    client_id: str | None = None
    client_secret: SecretRef | None = None
    scopes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _static_needs_a_value(self) -> Self:
        if self.mode == "static" and self.value is None:
            raise ValueError("outbound mode 'static' requires 'value'")
        return self

    @model_validator(mode="after")
    def _client_credentials_needs_endpoint_and_client(self) -> Self:
        if self.mode != "client_credentials":
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
            raise ValueError(
                f"outbound mode 'client_credentials' requires {missing}"
            )
        return self
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
git commit -m "feat: add client_credentials outbound mode to the config schema"
```

---

### Task 2: `PolicyDecision.carry` becomes the accumulated required-detail tuple

**Files:**
- Modify: `src/mcp_portal/policy.py`
- Modify: `tests/test_policy.py`

**Interfaces:**
- Consumes: `AuthorizationDetail` (existing, `auth/rar.py`); `PolicyRule`,
  `PolicyConfig` (existing, `config/policy.py`).
- Produces: `PolicyDecision.carry: tuple[AuthorizationDetail, ...] = ()`
  (was `bool = False`). `PolicyDecision.allowed` and `.missing` are
  unchanged.

- [ ] **Step 1: Write the failing tests**

In `tests/test_policy.py`, replace the two carry-related tests:

```python
def test_carry_is_the_accumulated_required_details_of_every_carrying_rule():
    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                {
                    "match": {"tags": ["billing"]},
                    "require": {"authorization_details": [{"type": "payment_initiation"}]},
                    "outbound": {"carry": True},
                }
            ],
        }
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal(AuthorizationDetail(type="payment_initiation")))
    assert decision.allowed is True
    assert decision.carry == (AuthorizationDetail(type="payment_initiation"),)


def test_carry_is_empty_when_no_matching_rule_sets_it():
    cfg = PolicyConfig.model_validate({"version": "1", "rules": [PAYMENT_RULE]})
    presented = AuthorizationDetail(
        type="payment_initiation",
        actions=("initiate",),
        locations=("https://api.example.com/v1/payments",),
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal(presented))
    assert decision.carry == ()


def test_a_carry_only_rule_carries_nothing_since_it_has_no_require():
    cfg = PolicyConfig.model_validate(
        {"version": "1", "rules": [{"match": {"tags": ["billing"]}, "outbound": {"carry": True}}]}
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal())
    assert decision.allowed is True
    assert decision.carry == ()


def test_carry_only_includes_details_from_rules_whose_own_outbound_carry_is_true():
    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                PAYMENT_RULE,  # carry defaults to False
                {
                    "match": {"tags": ["billing"]},
                    "require": {"authorization_details": [{"type": "audit_log"}]},
                    "outbound": {"carry": True},
                },
            ],
        }
    )
    presented = (
        AuthorizationDetail(
            type="payment_initiation",
            actions=("initiate",),
            locations=("https://api.example.com/v1/payments",),
        ),
        AuthorizationDetail(type="audit_log"),
    )
    decision = PolicyEngine(cfg).evaluate(op(), principal(*presented))
    assert decision.allowed is True
    assert decision.carry == (AuthorizationDetail(type="audit_log"),)
```

Remove the old `test_carry_is_true_when_any_matching_rule_sets_it` and
`test_carry_is_false_when_no_matching_rule_sets_it` (superseded above).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_policy.py -v`
Expected: FAIL — `decision.carry` is currently `bool`, comparisons against a
tuple fail.

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/policy.py`, factor the detail-conversion helper out of
`evaluate` and use it for both `required` and `carry`:

```python
def _accumulated_details(rules: Sequence[PolicyRule]) -> tuple[AuthorizationDetail, ...]:
    result: list[AuthorizationDetail] = []
    for rule in rules:
        if rule.require is None:
            continue
        result.extend(
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
    return tuple(result)
```

Replace `PolicyDecision` and `PolicyEngine.evaluate`:

```python
@dataclasses.dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    missing: tuple[AuthorizationDetail, ...] = ()
    carry: tuple[AuthorizationDetail, ...] = ()


class PolicyEngine:
    def __init__(self, policy: PolicyConfig) -> None:
        self._policy = policy

    def evaluate(self, operation: Operation, principal: Principal) -> PolicyDecision:
        matching = _matching_rules(operation, self._policy.rules)

        required = _accumulated_details(matching)
        carry = _accumulated_details([r for r in matching if r.outbound.carry])

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
Expected: all passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/policy.py tests/test_policy.py
git commit -m "feat: PolicyDecision.carry becomes the accumulated required-detail tuple"
```

---

### Task 3: The token cache (`auth/token_cache.py`)

**Files:**
- Create: `src/mcp_portal/auth/token_cache.py`
- Create: `tests/test_auth_token_cache.py`

**Interfaces:**
- Consumes: nothing from other P4 modules — pure `asyncio`/`time`, so it is
  shared unchanged by `client_credentials` (this plan) and `token_exchange`
  (this plan, then the sibling plan).
- Produces: `TokenCache` with `async get_or_fetch(key: str, fetch:
  Callable[[], Awaitable[tuple[str, float]]]) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth_token_cache.py`:

```python
import asyncio

import pytest

from mcp_portal.auth.token_cache import TokenCache


@pytest.mark.anyio
async def test_a_fresh_key_calls_fetch_once():
    calls = []

    async def fetch() -> tuple[str, float]:
        calls.append(1)
        return "token-1", 60.0

    cache = TokenCache()
    assert await cache.get_or_fetch("k", fetch) == "token-1"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_a_cached_key_within_ttl_does_not_refetch():
    calls = []

    async def fetch() -> tuple[str, float]:
        calls.append(1)
        return f"token-{len(calls)}", 60.0

    cache = TokenCache()
    await cache.get_or_fetch("k", fetch)
    assert await cache.get_or_fetch("k", fetch) == "token-1"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_an_expired_key_refetches():
    calls = []

    async def fetch() -> tuple[str, float]:
        calls.append(1)
        return f"token-{len(calls)}", 0.0  # expires immediately

    cache = TokenCache()
    await cache.get_or_fetch("k", fetch)
    await asyncio.sleep(0)
    assert await cache.get_or_fetch("k", fetch) == "token-2"
    assert len(calls) == 2


@pytest.mark.anyio
async def test_distinct_keys_do_not_share_an_entry():
    async def fetch_a() -> tuple[str, float]:
        return "a", 60.0

    async def fetch_b() -> tuple[str, float]:
        return "b", 60.0

    cache = TokenCache()
    assert await cache.get_or_fetch("a", fetch_a) == "a"
    assert await cache.get_or_fetch("b", fetch_b) == "b"


@pytest.mark.anyio
async def test_concurrent_callers_for_the_same_key_single_flight_the_fetch():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fetch() -> tuple[str, float]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "token", 60.0

    cache = TokenCache()
    first = asyncio.ensure_future(cache.get_or_fetch("k", fetch))
    await started.wait()
    second = asyncio.ensure_future(cache.get_or_fetch("k", fetch))
    await asyncio.sleep(0.01)  # let `second` reach and block on the same lock
    release.set()

    assert await first == "token"
    assert await second == "token"
    assert calls == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_auth_token_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.auth.token_cache'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/auth/token_cache.py`:

```python
"""A single-flighted, TTL-based cache for tokens acquired from an IdP.

Shared by every dynamic outbound mode (`client_credentials` now,
`token_exchange` later): both mint a token good for a window, keyed by
whatever the caller derives from upstream + scopes + carried details (and,
for exchange, the subject token). A burst of concurrent tool calls for the
same key must trigger exactly one IdP request, not one per caller — hence
the per-key lock rather than a bare dict.
"""

import time
from asyncio import Lock
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _CachedToken:
    value: str
    expires_at: float  # a time.monotonic() timestamp


class TokenCache:
    def __init__(self) -> None:
        self._entries: dict[str, _CachedToken] = {}
        self._locks: dict[str, Lock] = {}

    def _lock_for(self, key: str) -> Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = Lock()
            self._locks[key] = lock
        return lock

    async def get_or_fetch(
        self, key: str, fetch: Callable[[], Awaitable[tuple[str, float]]]
    ) -> str:
        """Return the cached token for `key`, fetching it if absent or expired.

        `fetch` returns `(token, ttl_seconds)`. Only the first caller to reach
        an absent-or-expired entry ever awaits `fetch`; every concurrent
        caller for the same key blocks on the same lock and then reads the
        entry that call just populated.
        """
        cached = self._entries.get(key)
        if cached is not None and cached.expires_at > time.monotonic():
            return cached.value

        async with self._lock_for(key):
            cached = self._entries.get(key)
            if cached is not None and cached.expires_at > time.monotonic():
                return cached.value
            token, ttl_seconds = await fetch()
            self._entries[key] = _CachedToken(value=token, expires_at=time.monotonic() + ttl_seconds)
            return token
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth_token_cache.py -v`
Expected: 5 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/auth/token_cache.py tests/test_auth_token_cache.py
git commit -m "feat: single-flighted TTL token cache for dynamic outbound modes"
```

---

### Task 4: `client_credentials` acquisition and the `CredentialSource` protocol

**Files:**
- Modify: `src/mcp_portal/transports/http.py`
- Modify: `src/mcp_portal/auth/outbound.py`
- Modify: `tests/test_outbound.py`
- Create: `tests/test_outbound_client_credentials.py`

**Interfaces:**
- Consumes: `AuthorizationDetail` (existing, `auth/rar.py`); `TokenCache`
  (Task 3); `OutboundConfig` (Task 1); `Credential` (existing,
  `transports/http.py`).
- Produces: `transports.http.CredentialSource` (a `Protocol`);
  `transports.http.StaticCredentialSource` (wraps a fixed `Credential |
  None`); `auth.outbound.credential_for` (existing, unchanged — still the
  synchronous `none`/`static` resolver); `auth.outbound.OutboundError`;
  `auth.outbound.ClientCredentialsSource(outbound, secrets, client, cache,
  upstream_key)` implementing `CredentialSource`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outbound_client_credentials.py`:

```python
import httpx
import pytest

from mcp_portal.auth.outbound import ClientCredentialsSource, OutboundError
from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.auth.token_cache import TokenCache
from mcp_portal.config.models import OutboundConfig


def cfg(**overrides) -> OutboundConfig:
    base = {
        "mode": "client_credentials",
        "token_endpoint": "https://idp.example.com/oauth2/token",
        "client_id": "sidekit-billing",
        "client_secret": "${env:SECRET}",
        "scopes": ["invoices.write"],
    }
    return OutboundConfig(**(base | overrides))


def source(handler, **cfg_overrides) -> ClientCredentialsSource:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ClientCredentialsSource(
        outbound=cfg(**cfg_overrides),
        secrets={"${env:SECRET}": "client-secret-value"},
        client=client,
        cache=TokenCache(),
        upstream_key="billing",
    )


@pytest.mark.anyio
async def test_a_successful_token_response_yields_a_bearer_credential():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        body = request.read().decode()
        assert "grant_type=client_credentials" in body
        assert "scope=invoices.write" in body
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    cred = await source(handler).get(carry=())
    assert cred is not None
    assert cred.header == "Authorization"
    assert cred.value == "Bearer tok-1"


@pytest.mark.anyio
async def test_carried_details_are_sent_as_authorization_details():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        import json
        from urllib.parse import parse_qs

        seen["body"] = parse_qs(request.read().decode())
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    detail = AuthorizationDetail(type="payment_initiation", actions=("initiate",))
    await source(handler).get(carry=(detail,))
    import json as _json

    sent = _json.loads(seen["body"]["authorization_details"][0])
    assert sent == [{"type": "payment_initiation", "actions": ["initiate"]}]


@pytest.mark.anyio
async def test_a_non_200_token_response_is_an_outbound_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client"})

    with pytest.raises(OutboundError):
        await source(handler).get(carry=())


@pytest.mark.anyio
async def test_a_response_missing_access_token_is_an_outbound_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"expires_in": 3600})

    with pytest.raises(OutboundError):
        await source(handler).get(carry=())


@pytest.mark.anyio
async def test_two_calls_within_ttl_reuse_the_cached_token():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": f"tok-{len(calls)}", "expires_in": 3600})

    src = source(handler)
    first = await src.get(carry=())
    second = await src.get(carry=())
    assert first.value == second.value
    assert len(calls) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_outbound_client_credentials.py -v`
Expected: FAIL — `ImportError: cannot import name 'ClientCredentialsSource'`

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/transports/http.py`, add near the top (after the imports,
before `Credential`):

```python
from typing import Protocol

from mcp_portal.auth.rar import AuthorizationDetail


class CredentialSource(Protocol):
    """Produces the credential to attach to one call.

    `carry` is the accumulated set of RAR details the matching policy rules
    said to carry (§8's `outbound.carry`) — empty when no rule asked for it.
    `subject_token` is the raw inbound bearer token, present only under
    `transport: http` with inbound auth enabled; every mode but
    `token_exchange` ignores it.
    """

    async def get(
        self, carry: tuple[AuthorizationDetail, ...], subject_token: str | None = None
    ) -> "Credential | None": ...
```

Add `StaticCredentialSource` directly after `Credential`:

```python
@dataclass(frozen=True, slots=True)
class StaticCredentialSource:
    """Wraps a `none`/`static` credential resolved once at startup.

    Neither mode varies per call, so `carry` and `subject_token` are accepted
    (to satisfy `CredentialSource`) and ignored.
    """

    credential: Credential | None

    async def get(
        self, carry: tuple[AuthorizationDetail, ...], subject_token: str | None = None
    ) -> Credential | None:
        return self.credential
```

Change `HttpTransport.__init__` and `execute`'s call to `build_request`:

```python
class HttpTransport:
    def __init__(
        self,
        client: httpx.AsyncClient,
        upstream: UpstreamConfig,
        credential_source: CredentialSource,
    ) -> None:
        self._client = client
        self._upstream = upstream
        self._credential_source = credential_source
```

```python
    async def execute(
        self,
        operation: Operation,
        arguments: dict[str, Any],
        carry: tuple[AuthorizationDetail, ...] = (),
    ) -> HttpResponse:
        binding = operation.binding
        assert isinstance(binding, HttpBinding)
        base_url = self._upstream.base_url
        assert base_url is not None
        credential = await self._credential_source.get(carry)
        request = build_request(binding, base_url, arguments, credential)
        # ... unchanged below this line
```

Now update every existing `HttpTransport(client, upstream, credential)` call
site to wrap the third argument: `HttpTransport(client, upstream,
StaticCredentialSource(credential))`. There are two in
`tests/test_http_execute.py`'s `transport()` helper and
`tests/test_server_mcp.py`'s `invoker()`/`invoker_with_policy()` helpers
(these were added by the P3 plan). Update each:

`tests/test_http_execute.py`:
```python
def transport(handler, credential: Credential | None = None, **upstream_kw) -> HttpTransport:
    upstream = UpstreamConfig(base_url="https://api.example.com", **upstream_kw)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpTransport(client=client, upstream=upstream, credential_source=StaticCredentialSource(credential))
```
(add `from mcp_portal.transports.http import StaticCredentialSource` to its imports)

`tests/test_server_mcp.py`, both `HttpTransport(client, upstream, None)` and
`HttpTransport(client, upstream, HttpTransport)`-shaped calls become
`HttpTransport(client, upstream, StaticCredentialSource(None))` (add the
same import).

In `src/mcp_portal/auth/outbound.py`, keep `credential_for` unchanged and add:

```python
import json
from dataclasses import dataclass

import httpx

from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.auth.token_cache import TokenCache
from mcp_portal.config.models import OutboundConfig
from mcp_portal.transports.http import Credential

_EXPIRY_LEEWAY_S = 60.0


class OutboundError(Exception):
    """Raised when acquiring a dynamic outbound credential fails."""


def _authorization_details_json(details: tuple[AuthorizationDetail, ...]) -> str | None:
    if not details:
        return None
    return json.dumps(
        [
            {
                k: v
                for k, v in {
                    "type": d.type,
                    "actions": list(d.actions) or None,
                    "locations": list(d.locations) or None,
                    "datatypes": list(d.datatypes) or None,
                    "identifier": d.identifier,
                    "privileges": list(d.privileges) or None,
                }.items()
                if v is not None
            }
            for d in details
        ]
    )


async def _post_token_request(
    client: httpx.AsyncClient,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    data: dict[str, str],
) -> tuple[str, float]:
    """POST a token request with HTTP Basic client authentication.

    Returns `(access_token, ttl_seconds)`. `ttl_seconds` already has the
    expiry leeway subtracted, so cache consumers never re-derive it.
    """
    response = await client.post(
        token_endpoint,
        data=data,
        auth=(client_id, client_secret),
        headers={"Accept": "application/json"},
    )
    if response.status_code != 200:
        raise OutboundError(
            f"token request to {token_endpoint!r} failed with {response.status_code}: "
            f"{response.text[:500]}"
        )
    payload = response.json()
    token = payload.get("access_token")
    if not isinstance(token, str):
        raise OutboundError(f"token response from {token_endpoint!r} has no 'access_token'")
    expires_in = payload.get("expires_in", 300)
    ttl = max(0.0, float(expires_in) - _EXPIRY_LEEWAY_S)
    return token, ttl


@dataclass(frozen=True, slots=True)
class ClientCredentialsSource:
    """RFC 6749 client credentials grant, cached per upstream+scopes+carry."""

    outbound: OutboundConfig
    secrets: dict[str, str]
    client: httpx.AsyncClient
    cache: TokenCache
    upstream_key: str

    def _client_secret(self) -> str:
        assert self.outbound.client_secret is not None
        try:
            return self.secrets[self.outbound.client_secret]
        except KeyError:
            raise OutboundError(
                f"secret reference {self.outbound.client_secret!r} was not resolved at load time"
            ) from None

    async def get(
        self, carry: tuple[AuthorizationDetail, ...], subject_token: str | None = None
    ) -> Credential | None:
        scopes = tuple(sorted(self.outbound.scopes))
        key = f"client_credentials:{self.upstream_key}:{scopes}:{hash(carry)}"

        async def fetch() -> tuple[str, float]:
            assert self.outbound.token_endpoint is not None
            assert self.outbound.client_id is not None
            data: dict[str, str] = {"grant_type": "client_credentials"}
            if scopes:
                data["scope"] = " ".join(scopes)
            details_json = _authorization_details_json(carry)
            if details_json is not None:
                data["authorization_details"] = details_json
            return await _post_token_request(
                self.client,
                self.outbound.token_endpoint,
                self.outbound.client_id,
                self._client_secret(),
                data,
            )

        token = await self.cache.get_or_fetch(key, fetch)
        return Credential(header="Authorization", value=f"Bearer {token}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
uv run pytest tests/test_outbound_client_credentials.py tests/test_outbound.py tests/test_http_execute.py tests/test_server_mcp.py -v
```
Expected: all passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/transports/http.py src/mcp_portal/auth/outbound.py \
        tests/test_outbound_client_credentials.py tests/test_outbound.py \
        tests/test_http_execute.py tests/test_server_mcp.py
git commit -m "feat: client_credentials outbound mode via a CredentialSource protocol"
```

---

### Task 5: The `token_exchange` acquisition function (standalone, unwired)

**Files:**
- Modify: `src/mcp_portal/auth/outbound.py`
- Create: `tests/test_outbound_token_exchange.py`

**Interfaces:**
- Consumes: `_post_token_request`-shaped POST logic (Task 4, adapted below
  since token exchange's grant parameters differ from client credentials'.
- Produces: `auth.outbound.TokenExchangeSource(token_endpoint, client_id,
  client_secret, audience, resource, requested_token_type, scopes, client,
  cache, upstream_key)` implementing `CredentialSource`. **Not** referenced
  by `config/models.py`, `app.py`, or `server/mcp.py` in this plan — the
  sibling P4-inbound plan wires it in once a real `subject_token` exists.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outbound_token_exchange.py`:

```python
import httpx
import pytest

from mcp_portal.auth.outbound import OutboundError, TokenExchangeSource
from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.auth.token_cache import TokenCache


def source(handler, **overrides) -> TokenExchangeSource:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    base = dict(
        token_endpoint="https://idp.example.com/oauth2/token",
        client_id="sidekit-billing",
        client_secret="client-secret-value",
        audience="https://api.example.com",
        requested_token_type="urn:ietf:params:oauth:token-type:access_token",
        scopes=["invoices.write"],
        client=client,
        cache=TokenCache(),
        upstream_key="billing",
    )
    return TokenExchangeSource(**(base | overrides))


@pytest.mark.anyio
async def test_a_successful_exchange_yields_a_bearer_credential():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange" in body
        assert "subject_token=inbound-jwt" in body
        assert "subject_token_type=urn%3Aietf%3Aparams%3Aoauth%3Atoken-type%3Aaccess_token" in body
        assert "audience=https%3A%2F%2Fapi.example.com" in body
        return httpx.Response(200, json={"access_token": "exchanged-tok", "expires_in": 900})

    cred = await source(handler).get(carry=(), subject_token="inbound-jwt")
    assert cred is not None
    assert cred.value == "Bearer exchanged-tok"


@pytest.mark.anyio
async def test_no_subject_token_is_an_outbound_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the token endpoint without a subject token")

    with pytest.raises(OutboundError, match="subject_token"):
        await source(handler).get(carry=(), subject_token=None)


@pytest.mark.anyio
async def test_two_distinct_subject_tokens_do_not_share_a_cache_entry():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": f"tok-{len(calls)}", "expires_in": 900})

    src = source(handler)
    first = await src.get(carry=(), subject_token="token-a")
    second = await src.get(carry=(), subject_token="token-b")
    assert first.value != second.value
    assert len(calls) == 2


@pytest.mark.anyio
async def test_the_same_subject_token_reuses_the_cached_exchanged_token():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"access_token": f"tok-{len(calls)}", "expires_in": 900})

    src = source(handler)
    first = await src.get(carry=(), subject_token="token-a")
    second = await src.get(carry=(), subject_token="token-a")
    assert first.value == second.value
    assert len(calls) == 1


@pytest.mark.anyio
async def test_carried_details_are_included_in_the_cache_key_and_the_request():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert "authorization_details" in body
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})

    detail = AuthorizationDetail(type="payment_initiation")
    await source(handler).get(carry=(detail,), subject_token="token-a")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_outbound_token_exchange.py -v`
Expected: FAIL — `ImportError: cannot import name 'TokenExchangeSource'`

- [ ] **Step 3: Write the implementation**

Append to `src/mcp_portal/auth/outbound.py`:

```python
@dataclass(frozen=True, slots=True)
class TokenExchangeSource:
    """RFC 8693 token exchange, using the inbound bearer token as the
    `subject_token`. Cached per upstream + hash(subject token) + scopes +
    carry — the subject token itself is part of the key (not its `sub`
    claim), since two distinct tokens for the same subject must never share
    an exchanged token: a revoked or expired one would keep working through
    an entry minted for the other (§8)."""

    token_endpoint: str
    client_id: str
    client_secret: str
    audience: str | None
    resource: str | None
    requested_token_type: str
    scopes: list[str]
    client: httpx.AsyncClient
    cache: TokenCache
    upstream_key: str

    async def get(
        self, carry: tuple[AuthorizationDetail, ...], subject_token: str | None = None
    ) -> Credential | None:
        if subject_token is None:
            raise OutboundError(
                "token_exchange requires an inbound 'subject_token'; this should have "
                "been rejected at startup for a transport/auth combination with no "
                "inbound token to exchange"
            )

        scopes = tuple(sorted(self.scopes))
        key = (
            f"token_exchange:{self.upstream_key}:{hash(subject_token)}:{scopes}:{hash(carry)}"
        )

        async def fetch() -> tuple[str, float]:
            data: dict[str, str] = {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": subject_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_token_type": self.requested_token_type,
            }
            if self.audience is not None:
                data["audience"] = self.audience
            if self.resource is not None:
                data["resource"] = self.resource
            if scopes:
                data["scope"] = " ".join(scopes)
            details_json = _authorization_details_json(carry)
            if details_json is not None:
                data["authorization_details"] = details_json
            return await _post_token_request(
                self.client, self.token_endpoint, self.client_id, self.client_secret, data
            )

        token = await self.cache.get_or_fetch(key, fetch)
        return Credential(header="Authorization", value=f"Bearer {token}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_outbound_token_exchange.py -v`
Expected: 5 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/auth/outbound.py tests/test_outbound_token_exchange.py
git commit -m "feat: standalone RFC 8693 token exchange acquisition (not yet wired into config)"
```

---

### Task 6: Wire `client_credentials` end to end

**Files:**
- Modify: `src/mcp_portal/server/mcp.py`
- Modify: `src/mcp_portal/app.py`
- Modify: `tests/test_server_mcp.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `CredentialSource`, `StaticCredentialSource` (Task 4);
  `ClientCredentialsSource` (Task 4); `TokenCache` (Task 3);
  `PolicyDecision.carry` (Task 2).
- Produces: `ToolInvoker.call` now passes `decision.carry` to
  `transport.execute`; `build_app` builds one `TokenCache` per app and one
  `CredentialSource` per upstream, `client_credentials` upstreams included.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_mcp.py`:

```python
@pytest.mark.anyio
async def test_a_carrying_rules_required_details_reach_the_credential_source():
    seen_carry = []

    class RecordingSource:
        async def get(self, carry, subject_token=None):
            seen_carry.append(carry)
            return None

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                {
                    "match": {"tags": ["billing"]},
                    "require": {"authorization_details": [{"type": "payment_initiation"}]},
                    "outbound": {"carry": True},
                }
            ],
        }
    )
    presented = AuthorizationDetail(type="payment_initiation")
    toolset = ToolSet(operations=(op(),), by_name={op().name: op()})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    invoker = ToolInvoker(
        toolset=toolset,
        transports={"billing": HttpTransport(client, UpstreamConfig(base_url="https://api.example.com"), RecordingSource())},
        policy=PolicyEngine(cfg),
        principal=Principal("local", (presented,)),
    )
    await invoker.call("list_invoices", {})
    assert seen_carry == [(presented,)]
```

Add `import dataclasses` if not already present (it is, from an earlier
task). Ensure `AuthorizationDetail` and `Principal` are imported (they
already are, from the P3 task).

Append to `tests/test_app.py` (check its existing fixtures/imports first —
mirror the style of its existing `test_build_app_*` tests):

```python
def test_client_credentials_upstream_builds_a_dynamic_credential_source(tmp_path):
    # Uses the same config-writing helper the rest of this file already
    # defines (e.g. `write_config` / `MINIMAL`-style fixture) — extend the
    # upstream's outbound block with:
    #   auth:
    #     outbound:
    #       mode: client_credentials
    #       token_endpoint: https://idp.example.com/oauth2/token
    #       client_id: sidekit-billing
    #       client_secret: ${env:BILLING_CLIENT_SECRET}
    # and assert build_app(...) succeeds and the resulting transport's
    # credential_source is an instance of ClientCredentialsSource.
    ...
```

Before writing this test for real, read `tests/test_app.py` in full and
match its existing config-construction helper and monkeypatch/env-var
pattern exactly — do not introduce a second way of building a `LoadedConfig`
in this file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_server_mcp.py tests/test_app.py -v`
Expected: FAIL — `ToolInvoker.call` does not yet pass `carry` to
`transport.execute`; `build_app` does not yet route `client_credentials` to
`ClientCredentialsSource`.

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/server/mcp.py`, change the transport call in `call`:

```python
        try:
            response = await transport.execute(operation, args, carry=decision.carry)
        except RequestBuildError as exc:
```

In `src/mcp_portal/app.py`, add the imports and replace the transport-building
loop:

```python
from mcp_portal.auth.outbound import ClientCredentialsSource, credential_for
from mcp_portal.auth.token_cache import TokenCache
from mcp_portal.transports.http import CredentialSource, StaticCredentialSource
```

```python
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
        else:
            credential_source = StaticCredentialSource(credential_for(outbound, loaded.secrets))
        transports[key] = HttpTransport(
            client=client,
            upstream=upstream.model_copy(update={"base_url": resolved_base_urls[key]}),
            credential_source=credential_source,
        )
```

`client_credentials` reuses the upstream's own `httpx.AsyncClient` for token
requests — it is a different host but the same client works cross-host, and
sharing it means `App.aclose()` (already iterating `_clients`) needs no
changes.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_server_mcp.py tests/test_app.py -v`
Expected: all passed.

- [ ] **Step 5: Run this plan's full test surface**

```bash
uv run pytest tests/test_config_models.py tests/test_policy.py \
              tests/test_auth_token_cache.py tests/test_outbound.py \
              tests/test_outbound_client_credentials.py tests/test_outbound_token_exchange.py \
              tests/test_http_execute.py tests/test_server_mcp.py tests/test_app.py \
              tests/test_schema_drift.py -v
```
Expected: all passed. (Do not run the full suite — later plans still need to
land; this is this plan's own scope per the Global Constraints.)

- [ ] **Step 6: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 7: Update README and AGENTS.md**

Per `AGENTS.md`'s Agent Guardrails, update `README.md`'s Status line and
Roadmap: `client_credentials` moves from "not implemented" to implemented;
`token_exchange`, inbound OAuth, and the HTTP transport stay in the Roadmap
(they are the sibling plan). Update `AGENTS.md`'s Repository Structure note
for `auth/outbound.py` and add `auth/token_cache.py` to the listing.

- [ ] **Step 8: Commit**

```bash
git add src/mcp_portal/server/mcp.py src/mcp_portal/app.py \
        tests/test_server_mcp.py tests/test_app.py README.md AGENTS.md
git commit -m "feat: wire client_credentials into the invocation path end to end"
```

---

## Self-Review Notes

- **Spec coverage:** §8's outbound table row for `client_credentials` is
  fully implemented and wired (Tasks 1, 4, 6). The `token_exchange` row's
  acquisition logic is implemented and unit-tested (Task 5) per this plan's
  explicit scope boundary; its config literal, startup validation, and
  runtime wiring are the sibling P4-inbound plan, which depends on this
  plan's `TokenCache`, `CredentialSource` protocol, and `TokenExchangeSource`.
  The token cache table (§8) is implemented for both modes (Tasks 3–5). The
  `carry` semantics (§8, "What `carry: true` carries") are implemented in
  Task 2.
- **Not in this plan, by design:** `auth/inbound.py`, `server/http.py`,
  `transport: http`, RFC 9728 discovery, JWKS, Origin validation — all
  sibling-plan scope.
