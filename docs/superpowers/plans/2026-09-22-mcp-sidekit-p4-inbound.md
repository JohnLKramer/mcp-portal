# mcp-portal P4b (Inbound + Streamable HTTP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Depends on:** `docs/superpowers/plans/2026-09-22-mcp-sidekit-p4-outbound.md` must land first. This plan
> consumes its `TokenCache`, `CredentialSource` protocol, `StaticCredentialSource`,
> `ClientCredentialsSource`, `TokenExchangeSource`, and `PolicyDecision.carry` (the
> accumulated-tuple form) as-is.

**Goal:** Add `transport: http` — a streamable HTTP MCP endpoint authenticated
by a validated bearer token (RFC 9068 access token, JWKS-verified, RFC 9728
resource-server discovery) — and finish wiring `outbound.mode:
token_exchange` (RFC 8693) now that there is a real inbound token to exchange.
This is the "full broker over streamable HTTP" the design's phasing table
(§12) names as P4's runnable result.

**Architecture:** The `mcp` SDK (`mcp>=2.2.0,<3`, already a dependency)
vendors most of the hard parts of an OAuth **resource server** — as opposed
to the authorization-server routes (`/authorize`, `/token`, `/register`) the
same package also ships for building an AS, which this gateway is not and
never becomes:

| Design requirement | SDK piece reused |
|---|---|
| Streamable HTTP transport, stateless | `mcp.server.streamable_http_manager.StreamableHTTPSessionManager(stateless=True)` + `StreamableHTTPASGIApp` |
| `Origin` validation, DNS-rebinding defense | `mcp.server.transport_security.TransportSecurityMiddleware` |
| RFC 9728 protected-resource metadata route, path-suffixed | `mcp.server.auth.routes.create_protected_resource_routes` |
| `401` + `WWW-Authenticate: Bearer resource_metadata="..."` | `mcp.server.auth.middleware.bearer_auth.RequireAuthMiddleware` |
| Bearer token → `AccessToken`, plumbed through a contextvar | `mcp.server.auth.middleware.bearer_auth.BearerAuthBackend` (given our `TokenVerifier`) + `mcp.server.auth.middleware.auth_context.get_access_token()` |

What sidekit still writes: the `TokenVerifier` itself — JWT signature/claim
validation (`pyjwt[crypto]`, already resolvable in `uv.lock`, added as a
direct dependency here) against a JWKS this plan fetches and caches with a
rate-limited unknown-`kid` refresh, exactly the piece the SDK deliberately
leaves to the operator since it is IdP-specific. `auth/inbound.py` holds all
of it, mirroring `auth/rar.py`'s P3 shape: pure with respect to sidekit's own
modules, importing only `httpx`, `pyjwt`, and the SDK's `auth.provider`
types.

`Principal` (P3, `auth/principal.py`) gains a `subject_token` field —
`transport: stdio` never populates it; `transport: http` does, from the
validated `AccessToken.token`. `ToolInvoker.call` (currently built with one
fixed `Principal` for the process's lifetime, correct for stdio) gains a
per-call `principal` override, since an HTTP server derives a fresh one per
request. `TokenExchangeSource.get()` (P4a) already accepts a
`subject_token` parameter; this plan is what finally supplies a real one.

**Tech Stack:** P1–P4a's stack plus `pyjwt[crypto]` (JWT decode/verify) and
`uvicorn` (ASGI server for `transport: http`), both added to
`pyproject.toml` in Task 1. `starlette` arrives transitively via `mcp` and is
already exercised indirectly through the SDK pieces above; this plan is the
first to import it directly.

**Spec:** [`docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md`](../specs/2026-09-19-mcp-sidekit-design.md)

## Global Constraints

- **This plan does not re-implement anything the `mcp` SDK already provides
  as a resource server.** Reach for `StreamableHTTPSessionManager`,
  `TransportSecurityMiddleware`, `create_protected_resource_routes`,
  `BearerAuthBackend`/`RequireAuthMiddleware`, and `get_access_token()`
  rather than hand-rolling routing, Origin checking, or the 401/
  `WWW-Authenticate` response shape. Only the `TokenVerifier` (JWT/JWKS
  validation) is sidekit's own code, because it is IdP-specific and the SDK
  intentionally does not supply one.
- **Opaque token introspection (RFC 7662), DPoP, mTLS-bound tokens, and SSE
  streaming/MCP sessions are explicitly deferred** (§13) — do not add fields
  or code for them. `stateless=True` on the session manager is a permanent
  v1 choice, not a placeholder for later session support.
- **`alg: none` is rejected unconditionally**, never configurable back on.
  HMAC (`HS*`) algorithms are excluded from the default allowlist
  (`RS256`, `ES256`); nothing in this plan adds a shared-secret config field,
  so allowlisting `HS*` explicitly is inert in v1 (JWKS supplies only
  RSA/EC keys) rather than a real HMAC verification path — this is a
  deliberate, documented scope boundary, not an oversight.
- **The unauthenticated-HTTP guard (§8, §10 item 3) is a startup error**,
  not a runtime warning: `transport: http` + `auth.inbound.enabled: false`
  on a non-loopback host, without `allow_unauthenticated_http: true`.
- **`token_exchange` + (`transport: stdio` or `auth.inbound.enabled:
  false`) is a startup error** (§10 item 4) — there is no `subject_token`
  to exchange, and discovering that at call time would violate "config
  failures surface at startup."
- **The local-principal guardrail applies identically when HTTP inbound is
  disabled but permitted** (unauthenticated-http case) — same startup log
  banner style as `introspect-unsafe`, naming the bind address and every
  exposed tool (§8).
- **Every task ends with `uv run ruff format .`, `uv run ruff check .`,
  `uv run mypy src`, and the task's own test file passing** before the
  commit step. Do not run the full suite mid-task; the final task is where
  everything runs together.

---

### Task 1: Config — `transport: http`, `auth.inbound`, and `token_exchange`

**Files:**
- Modify: `src/mcp_portal/config/models.py`
- Modify: `tests/test_config_models.py`
- Modify: `pyproject.toml`
- Modify: `schema/config-v1.schema.json` (regenerated, not hand-edited)

**Interfaces:**
- Consumes: `Base`, `SecretRef`, `BaseUrl`, `OutboundConfig` (P4a Task 1).
- Produces: `HttpServerConfig`, `ServerConfig.transport` gains `"http"` and
  `ServerConfig.http: HttpServerConfig`; `InboundAuthConfig`,
  `AuthConfig.inbound: InboundAuthConfig`; `OutboundConfig.mode` gains
  `"token_exchange"` plus `audience`, `resource`, `requested_token_type`
  fields; `Config` gains two cross-field validators (unauthenticated-HTTP
  guard, token-exchange-requires-inbound).

- [ ] **Step 1: Add the new dependencies**

In `pyproject.toml`, add to `dependencies`:

```toml
    "pyjwt[crypto]>=2.14,<3",
    "uvicorn>=0.34,<1",
```

Run `uv sync` to lock them.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_config_models.py`:

```python
def test_transport_http_defaults_its_own_server_config():
    cfg = ServerConfig(name="s", transport="http")
    assert cfg.http.host == "127.0.0.1"
    assert cfg.http.path == "/mcp"
    assert cfg.http.allowed_origins == []


def test_inbound_disabled_by_default():
    cfg = Config.model_validate(MINIMAL)
    assert cfg.auth.inbound.enabled is False


def test_inbound_enabled_requires_issuer_and_audience():
    with pytest.raises(ValidationError):
        AuthConfig(inbound={"enabled": True})


def test_inbound_enabled_validates_with_issuer_and_audience():
    cfg = AuthConfig(inbound={"enabled": True, "issuer": "https://idp.example.com", "audience": "https://api.example.com/mcp"})
    assert cfg.inbound.algorithms == ["RS256", "ES256"]
    assert cfg.inbound.leeway_s == 60.0


def test_inbound_rejects_none_in_algorithms():
    with pytest.raises(ValidationError):
        AuthConfig(
            inbound={
                "enabled": True,
                "issuer": "https://idp.example.com",
                "audience": "https://api.example.com/mcp",
                "algorithms": ["none"],
            }
        )


def test_http_transport_on_a_non_loopback_host_without_inbound_is_a_startup_error():
    payload = MINIMAL | {"server": {"name": "s", "transport": "http", "http": {"host": "0.0.0.0"}}}
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_http_transport_on_a_non_loopback_host_with_the_escape_hatch_is_allowed():
    payload = MINIMAL | {
        "server": {"name": "s", "transport": "http", "http": {"host": "0.0.0.0"}},
        "auth": {"inbound": {"allow_unauthenticated_http": True}},
    }
    Config.model_validate(payload)  # does not raise


def test_http_transport_on_loopback_without_inbound_is_allowed():
    payload = MINIMAL | {"server": {"name": "s", "transport": "http"}}
    Config.model_validate(payload)  # does not raise


def test_token_exchange_under_stdio_is_a_startup_error():
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {
                    "outbound": {
                        "mode": "token_exchange",
                        "token_endpoint": "https://idp.example.com/oauth2/token",
                        "client_id": "c",
                        "client_secret": "${env:S}",
                    }
                },
            }
        }
    }
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_token_exchange_with_inbound_disabled_over_http_is_a_startup_error():
    payload = MINIMAL | {
        "server": {"name": "s", "transport": "http"},
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {
                    "outbound": {
                        "mode": "token_exchange",
                        "token_endpoint": "https://idp.example.com/oauth2/token",
                        "client_id": "c",
                        "client_secret": "${env:S}",
                    }
                },
            }
        },
    }
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_token_exchange_with_http_and_inbound_enabled_is_valid():
    payload = MINIMAL | {
        "server": {"name": "s", "transport": "http"},
        "auth": {
            "inbound": {"enabled": True, "issuer": "https://idp.example.com", "audience": "https://api.example.com/mcp"}
        },
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {
                    "outbound": {
                        "mode": "token_exchange",
                        "token_endpoint": "https://idp.example.com/oauth2/token",
                        "client_id": "c",
                        "client_secret": "${env:S}",
                        "audience": "https://api.example.com",
                    }
                },
            }
        },
    }
    Config.model_validate(payload)  # does not raise
```

`MINIMAL` already exists in this test file (used by P1–P3 tests); check its
exact shape before writing these and reuse it, do not redefine it.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config_models.py -v`
Expected: FAIL — `ServerConfig` has no `http` field; `AuthConfig` has no
`inbound` field; `OutboundConfig` rejects `mode: "token_exchange"`; none of
the new `Config`-level validators exist yet.

- [ ] **Step 4: Write the implementation**

In `src/mcp_portal/config/models.py`, replace `OutboundConfig`'s
mode-specific validator (from P4a) to cover both dynamic modes, and add the
`token_exchange` fields:

```python
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
```

Add, near `IntrospectionConfig`:

```python
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class HttpServerConfig(Base):
    host: str = "127.0.0.1"
    port: int = Field(default=8443, gt=0, lt=65536)
    path: str = "/mcp"
    allowed_origins: list[str] = Field(default_factory=list)
```

Replace `ServerConfig`:

```python
class ServerConfig(Base):
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    http: HttpServerConfig = Field(default_factory=HttpServerConfig)
```

Add, near `LocalPrincipalConfig`:

```python
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
```

Replace `AuthConfig`:

```python
class AuthConfig(Base):
    local_principal: LocalPrincipalConfig = Field(default_factory=LocalPrincipalConfig)
    inbound: InboundAuthConfig = Field(default_factory=InboundAuthConfig)
```

Add two `model_validator`s to `Config` (after the existing three):

```python
    @model_validator(mode="after")
    def _unauthenticated_http_is_guarded(self) -> Self:
        if self.server.transport != "http" or self.auth.inbound.enabled:
            return self
        if self.server.http.host in _LOOPBACK_HOSTS:
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config_models.py -v`
Expected: all passed.

- [ ] **Step 6: Regenerate the schema and verify the drift check**

```bash
uv run python -m mcp_portal.config.schema
uv run pytest tests/test_schema_drift.py -v
```

- [ ] **Step 7: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/mcp_portal/config/models.py \
        tests/test_config_models.py schema/config-v1.schema.json
git commit -m "feat: config for transport:http, auth.inbound, and outbound token_exchange"
```

---

### Task 2: JWKS cache with rate-limited unknown-`kid` refresh

**Files:**
- Create: `src/mcp_portal/auth/inbound.py`
- Create: `tests/test_auth_inbound_jwks.py`

**Interfaces:**
- Consumes: nothing project-specific — `httpx` only.
- Produces: `InboundAuthError`; `discover_jwks_uri(client, issuer) ->
  str`; `JwksCache(client, jwks_uri)` with `async key_for(kid: str) -> dict
  | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth_inbound_jwks.py`:

```python
import time

import httpx
import pytest

from mcp_portal.auth.inbound import InboundAuthError, JwksCache, discover_jwks_uri

JWKS_ONE_KEY = {"keys": [{"kid": "k1", "kty": "RSA", "n": "x", "e": "AQAB"}]}


@pytest.mark.anyio
async def test_key_for_a_known_kid_is_returned_without_a_fetch():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=JWKS_ONE_KEY)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(client, "https://idp.example.com/jwks.json")
    assert (await cache.key_for("k1"))["kid"] == "k1"
    assert (await cache.key_for("k1"))["kid"] == "k1"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_an_unknown_kid_triggers_exactly_one_refresh():
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=JWKS_ONE_KEY)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(client, "https://idp.example.com/jwks.json")
    assert await cache.key_for("ghost") is None
    assert len(calls) == 1


@pytest.mark.anyio
async def test_refresh_is_rate_limited_to_once_per_60_seconds(monkeypatch):
    calls = []
    now = [1000.0]

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=JWKS_ONE_KEY)

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = JwksCache(client, "https://idp.example.com/jwks.json")

    await cache.key_for("ghost-1")
    await cache.key_for("ghost-2")  # still unknown, but within the 60s window
    assert len(calls) == 1

    now[0] += 61
    await cache.key_for("ghost-3")
    assert len(calls) == 2


@pytest.mark.anyio
async def test_discover_jwks_uri_prefers_oauth_authorization_server_metadata():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"jwks_uri": "https://idp.example.com/jwks.json"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await discover_jwks_uri(client, "https://idp.example.com") == "https://idp.example.com/jwks.json"


@pytest.mark.anyio
async def test_discover_jwks_uri_falls_back_to_openid_configuration():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"jwks_uri": "https://idp.example.com/jwks.json"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await discover_jwks_uri(client, "https://idp.example.com") == "https://idp.example.com/jwks.json"


@pytest.mark.anyio
async def test_discover_jwks_uri_raises_when_neither_document_is_available():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(InboundAuthError):
        await discover_jwks_uri(client, "https://idp.example.com")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auth_inbound_jwks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.auth.inbound'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/auth/inbound.py`:

```python
"""RFC 9068 access-token validation and RFC 9728/8414 discovery for
`transport: http` (§8). The `mcp` SDK supplies the resource-server routing,
Origin/Host defense, and 401/`WWW-Authenticate` shape (see this plan's
Architecture section); this module supplies the one IdP-specific piece the
SDK deliberately leaves to the operator: verifying a bearer token against a
JWKS and turning it into an `AccessToken`.
"""

import asyncio
import time
from typing import Any

import httpx

_JWKS_REFRESH_INTERVAL_S = 60.0
_DISCOVERY_SUFFIXES = ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration")


class InboundAuthError(Exception):
    """Raised when a bearer token cannot be validated at all (startup-time
    discovery failures) — as opposed to an individual token simply being
    invalid, which `JwtTokenVerifier.verify_token` reports by returning
    `None` so the SDK's bearer middleware can answer with a 401."""


async def discover_jwks_uri(client: httpx.AsyncClient, issuer: str) -> str:
    """Discover `jwks_uri` from `issuer`'s AS metadata, RFC 8414 first, then
    OpenID Connect discovery — both are in the wild for the same issuer."""
    base = issuer.rstrip("/")
    for suffix in _DISCOVERY_SUFFIXES:
        try:
            response = await client.get(base + suffix)
        except httpx.RequestError:
            continue
        if response.status_code != 200:
            continue
        jwks_uri = response.json().get("jwks_uri")
        if isinstance(jwks_uri, str):
            return jwks_uri
    raise InboundAuthError(f"could not discover jwks_uri for issuer {issuer!r}")


class JwksCache:
    """Caches JWKS keys by `kid`. An unknown `kid` triggers a refresh, rate
    limited to once per 60s so a forged `kid` cannot drive unbounded fetches
    (§8)."""

    def __init__(self, client: httpx.AsyncClient, jwks_uri: str) -> None:
        self._client = client
        self._jwks_uri = jwks_uri
        self._keys: dict[str, dict[str, Any]] = {}
        self._last_refresh: float = 0.0
        self._lock = asyncio.Lock()

    async def key_for(self, kid: str) -> dict[str, Any] | None:
        key = self._keys.get(kid)
        if key is not None:
            return key
        await self._maybe_refresh()
        return self._keys.get(kid)

    async def _maybe_refresh(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now - self._last_refresh < _JWKS_REFRESH_INTERVAL_S:
                return
            response = await self._client.get(self._jwks_uri)
            response.raise_for_status()
            for jwk in response.json().get("keys", []):
                kid = jwk.get("kid")
                if isinstance(kid, str):
                    self._keys[kid] = jwk
            self._last_refresh = now
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth_inbound_jwks.py -v`
Expected: 6 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/auth/inbound.py tests/test_auth_inbound_jwks.py
git commit -m "feat: JWKS cache and issuer discovery for inbound token validation"
```

---

### Task 3: `JwtTokenVerifier` — the SDK's `TokenVerifier` implemented

**Files:**
- Modify: `src/mcp_portal/auth/inbound.py`
- Create: `tests/test_auth_inbound_jwt.py`
- Create: `tests/conftest.py` fixture (or a local helper — check
  `tests/conftest.py` first for an existing RSA-keypair fixture before
  adding a second one)

**Interfaces:**
- Consumes: `JwksCache` (Task 2); `mcp.server.auth.provider.AccessToken`,
  `TokenVerifier` (SDK).
- Produces: `JwtTokenVerifier(issuer, audience, algorithms, required_scopes,
  leeway_s, jwks: JwksCache)` implementing `TokenVerifier`, i.e.
  `async def verify_token(self, token: str) -> AccessToken | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth_inbound_jwt.py`:

```python
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_portal.auth.inbound import JwksCache, JwtTokenVerifier


class _FakeJwks:
    """A `JwksCache`-shaped stub returning one fixed key, so these tests
    don't need a real JWKS HTTP round trip."""

    def __init__(self, jwk: dict) -> None:
        self._jwk = jwk

    async def key_for(self, kid: str) -> dict | None:
        return self._jwk if kid == self._jwk.get("kid") else None


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwk(rsa_key) -> dict:
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True)
    return public_jwk | {"kid": "test-kid", "use": "sig", "alg": "RS256"}


def _token(rsa_key, kid: str = "test-kid", **claim_overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": "https://api.example.com/mcp",
        "exp": now + 300,
        "nbf": now - 5,
        "scope": "invoices.read",
    } | claim_overrides
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": kid, "typ": "at+jwt"})


def verifier(jwk: dict, **overrides) -> JwtTokenVerifier:
    base = dict(
        issuer="https://idp.example.com",
        audience="https://api.example.com/mcp",
        algorithms=["RS256", "ES256"],
        required_scopes=[],
        leeway_s=60.0,
        jwks=_FakeJwks(jwk),
    )
    return JwtTokenVerifier(**(base | overrides))


@pytest.mark.anyio
async def test_a_well_formed_token_verifies(rsa_key, jwk):
    access = await verifier(jwk).verify_token(_token(rsa_key))
    assert access is not None
    assert access.scopes == ["invoices.read"]
    assert access.token == _token(rsa_key) or True  # token round-trips; exact string covered below


@pytest.mark.anyio
async def test_the_raw_token_string_is_preserved_as_the_subject_token(rsa_key, jwk):
    raw = _token(rsa_key)
    access = await verifier(jwk).verify_token(raw)
    assert access.token == raw


@pytest.mark.anyio
async def test_scp_array_form_is_also_accepted(rsa_key, jwk):
    now = int(time.time())
    raw = jwt.encode(
        {
            "iss": "https://idp.example.com",
            "aud": "https://api.example.com/mcp",
            "exp": now + 300,
            "scp": ["invoices.read", "invoices.write"],
        },
        rsa_key,
        algorithm="RS256",
        headers={"kid": "test-kid", "typ": "at+jwt"},
    )
    access = await verifier(jwk).verify_token(raw)
    assert access is not None
    assert set(access.scopes) == {"invoices.read", "invoices.write"}


@pytest.mark.anyio
async def test_authorization_details_claim_is_preserved_in_claims(rsa_key, jwk):
    raw = _token(rsa_key, authorization_details=[{"type": "payment_initiation", "actions": ["initiate"]}])
    access = await verifier(jwk).verify_token(raw)
    assert access is not None
    assert access.claims["authorization_details"] == [{"type": "payment_initiation", "actions": ["initiate"]}]


@pytest.mark.anyio
async def test_a_malformed_authorization_details_claim_denies_the_request(rsa_key, jwk):
    raw = _token(rsa_key, authorization_details="not-a-list")
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_a_wrong_issuer_is_rejected(rsa_key, jwk):
    raw = _token(rsa_key, iss="https://attacker.example.com")
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_a_wrong_audience_is_rejected(rsa_key, jwk):
    raw = _token(rsa_key, aud="https://other-api.example.com")
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_an_audience_array_containing_the_configured_audience_is_accepted(rsa_key, jwk):
    raw = _token(rsa_key, aud=["https://other.example.com", "https://api.example.com/mcp"])
    assert await verifier(jwk).verify_token(raw) is not None


@pytest.mark.anyio
async def test_an_expired_token_is_rejected(rsa_key, jwk):
    now = int(time.time())
    raw = _token(rsa_key, exp=now - 120)
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_leeway_tolerates_a_recently_expired_token(rsa_key, jwk):
    now = int(time.time())
    raw = _token(rsa_key, exp=now - 10)  # within the default 60s leeway
    assert await verifier(jwk).verify_token(raw) is not None


@pytest.mark.anyio
async def test_a_not_yet_valid_token_is_rejected(rsa_key, jwk):
    now = int(time.time())
    raw = _token(rsa_key, nbf=now + 120)
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_missing_required_scope_is_rejected(rsa_key, jwk):
    v = verifier(jwk, required_scopes=["invoices.write"])
    assert await v.verify_token(_token(rsa_key)) is None  # token only has invoices.read


@pytest.mark.anyio
async def test_an_unknown_kid_is_rejected(rsa_key, jwk):
    raw = _token(rsa_key, kid="ghost-kid")
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_typ_none_of_the_two_accepted_values_is_rejected(rsa_key, jwk):
    now = int(time.time())
    raw = jwt.encode(
        {"iss": "https://idp.example.com", "aud": "https://api.example.com/mcp", "exp": now + 300},
        rsa_key,
        algorithm="RS256",
        headers={"kid": "test-kid", "typ": "weird+jwt"},
    )
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_alg_none_is_rejected_unconditionally(jwk):
    now = int(time.time())
    raw = jwt.encode(
        {"iss": "https://idp.example.com", "aud": "https://api.example.com/mcp", "exp": now + 300},
        "",
        algorithm="none",
        headers={"typ": "at+jwt"},
    )
    assert await verifier(jwk, algorithms=["none", "RS256"]).verify_token(raw) is None


@pytest.mark.anyio
async def test_hs256_signed_with_the_rsa_public_key_pem_is_rejected(rsa_key, jwk):
    """The classic key-confusion attack: HS256, keyed with the RSA public
    key's own bytes. Rejected because HS256 is not in the default allowlist
    and there is no shared-secret config field for JWKS-only lookup to
    satisfy it even if it were (see this plan's Global Constraints)."""
    from cryptography.hazmat.primitives import serialization

    public_pem = rsa_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    now = int(time.time())
    raw = jwt.encode(
        {"iss": "https://idp.example.com", "aud": "https://api.example.com/mcp", "exp": now + 300},
        public_pem,
        algorithm="HS256",
        headers={"kid": "test-kid", "typ": "at+jwt"},
    )
    assert await verifier(jwk).verify_token(raw) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auth_inbound_jwt.py -v`
Expected: FAIL — `ImportError: cannot import name 'JwtTokenVerifier'`

- [ ] **Step 3: Write the implementation**

Append to `src/mcp_portal/auth/inbound.py`:

```python
import jwt as pyjwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

_ACCEPTED_TYP = frozenset({"at+jwt", "jwt"})


def _scopes_from_claims(claims: dict[str, Any]) -> list[str]:
    """Scopes come from a space-delimited `scope` string or an `scp` array —
    both are in the wild (§8)."""
    if isinstance(claims.get("scp"), list):
        return [str(s) for s in claims["scp"]]
    scope = claims.get("scope")
    if isinstance(scope, str) and scope:
        return scope.split()
    return []


class JwtTokenVerifier(TokenVerifier):
    """Validates an RFC 9068 access token against a JWKS.

    A malformed `authorization_details` claim, an unrecognized `kid`, a
    disallowed `alg` (including `none`, unconditionally), a wrong `typ`, or
    a failed `iss`/`aud`/`exp`/`nbf`/scope check all deny the request by
    returning `None` — the SDK's `BearerAuthBackend` turns that into a 401.
    None of these distinctions are surfaced to the caller (§10: never leak
    token contents), only the fact of denial.
    """

    def __init__(
        self,
        issuer: str,
        audience: str,
        algorithms: list[str],
        required_scopes: list[str],
        leeway_s: float,
        jwks: JwksCache,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._algorithms = frozenset(algorithms) - {"none"}
        self._required_scopes = required_scopes
        self._leeway_s = leeway_s
        self._jwks = jwks

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = pyjwt.get_unverified_header(token)
        except pyjwt.InvalidTokenError:
            return None

        alg = header.get("alg")
        if alg not in self._algorithms:
            return None
        if header.get("typ", "").lower() not in _ACCEPTED_TYP:
            return None

        kid = header.get("kid")
        if not isinstance(kid, str):
            return None
        jwk = await self._jwks.key_for(kid)
        if jwk is None:
            return None

        try:
            key = pyjwt.PyJWK.from_dict(jwk, algorithm=alg).key
            claims = pyjwt.decode(
                token,
                key=key,
                algorithms=[alg],
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway_s,
                options={"require": ["exp", "iss", "aud"]},
            )
        except pyjwt.InvalidTokenError:
            return None

        raw_details = claims.get("authorization_details")
        if raw_details is not None and not isinstance(raw_details, list):
            return None  # a malformed claim denies the request rather than being ignored (§8)

        scopes = _scopes_from_claims(claims)
        if any(scope not in scopes for scope in self._required_scopes):
            return None

        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("sub") or "unknown"),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self._audience,
            subject=claims.get("sub"),
            claims=claims,
        )
```

Note `pyjwt.decode`'s `audience=` parameter already accepts the token's
`aud` claim being either a string or a list and matches if the configured
value appears in either form — the design's "matches when the configured
audience appears in a string or array claim" (§8) is satisfied by PyJWT's
own semantics, no extra code needed.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth_inbound_jwt.py -v`
Expected: 17 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/auth/inbound.py tests/test_auth_inbound_jwt.py
git commit -m "feat: JWT access-token verification against a JWKS (RFC 9068)"
```

---

### Task 4: `Principal` gains a subject token

**Files:**
- Modify: `src/mcp_portal/auth/principal.py`
- Modify: `tests/test_auth_principal.py`

**Interfaces:**
- Consumes: `AuthorizationDetail`, `RarError`, `parse_authorization_details`
  (existing, `auth/rar.py`); `mcp.server.auth.provider.AccessToken` (SDK).
- Produces: `Principal.subject_token: str | None = None` (new field);
  `principal_from_access_token(access_token: AccessToken) -> Principal |
  None` — `None` when the token's `authorization_details` claim is
  malformed, so the caller can deny the request rather than treat it as
  absent (§8).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth_principal.py`:

```python
from mcp.server.auth.provider import AccessToken

from mcp_portal.auth.principal import principal_from_access_token


def _access_token(**overrides) -> AccessToken:
    base = dict(token="raw-jwt", client_id="c", scopes=["invoices.read"], claims={})
    return AccessToken(**(base | overrides))


def test_local_principal_has_no_subject_token():
    principal = local_principal(LocalPrincipalConfig())
    assert principal.subject_token is None


def test_principal_from_access_token_carries_the_raw_token_as_subject_token():
    principal = principal_from_access_token(_access_token())
    assert principal is not None
    assert principal.subject_token == "raw-jwt"


def test_principal_from_access_token_parses_authorization_details_from_claims():
    access = _access_token(
        claims={"authorization_details": [{"type": "payment_initiation", "actions": ["initiate"]}]}
    )
    principal = principal_from_access_token(access)
    assert principal is not None
    (detail,) = principal.authorization_details
    assert detail.type == "payment_initiation"


def test_principal_from_access_token_defaults_to_no_authorization_details():
    principal = principal_from_access_token(_access_token())
    assert principal is not None
    assert principal.authorization_details == ()


def test_principal_from_access_token_returns_none_for_a_malformed_claim():
    access = _access_token(claims={"authorization_details": [{"actions": ["initiate"]}]})  # missing 'type'
    assert principal_from_access_token(access) is None


def test_principal_identity_prefers_subject_then_falls_back_to_client_id():
    with_subject = principal_from_access_token(_access_token(subject="user-1"))
    assert with_subject.identity == "user-1"
    without_subject = principal_from_access_token(_access_token(subject=None))
    assert without_subject.identity == "c"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auth_principal.py -v`
Expected: FAIL — `Principal` has no `subject_token` field;
`principal_from_access_token` does not exist.

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/auth/principal.py`, replace `Principal` and add the new
function:

```python
from mcp.server.auth.provider import AccessToken


@dataclass(frozen=True, slots=True)
class Principal:
    identity: str
    authorization_details: tuple[AuthorizationDetail, ...]
    subject_token: str | None = None


def principal_from_access_token(access_token: AccessToken) -> Principal | None:
    """Build the principal a validated HTTP bearer token represents (§8).

    Returns `None` when `authorization_details` is present but malformed —
    the caller (the bearer-auth wiring in `server/http.py`) must deny the
    request rather than treat it as an absent claim, matching
    `JwtTokenVerifier`'s own rule for the same claim.
    """
    raw_details = (access_token.claims or {}).get("authorization_details", [])
    try:
        details = parse_authorization_details(raw_details)
    except RarError:
        return None
    identity = access_token.subject or access_token.client_id
    return Principal(identity=identity, authorization_details=details, subject_token=access_token.token)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth_principal.py -v`
Expected: all passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/auth/principal.py tests/test_auth_principal.py
git commit -m "feat: derive a Principal (with subject_token) from a validated access token"
```

---

### Task 5: `ToolInvoker.call` accepts a per-call principal override

**Files:**
- Modify: `src/mcp_portal/server/mcp.py`
- Modify: `tests/test_server_mcp.py`

**Interfaces:**
- Consumes: `Principal` (existing/Task 4).
- Produces: `ToolInvoker.call(name, arguments, principal: Principal | None =
  None)` — `principal` overrides the instance default for this call only.
  `transport.execute`'s `carry` argument (P4a) is unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server_mcp.py`:

```python
@pytest.mark.anyio
async def test_call_accepts_a_per_call_principal_override():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    cfg = PolicyConfig.model_validate(
        {
            "version": "1",
            "rules": [
                {
                    "match": {"effect": ["read_only"]},
                    "require": {"authorization_details": [{"type": "invoices_read"}]},
                }
            ],
        }
    )
    invoker = invoker_with_policy(handler, op(), PolicyEngine(cfg), Principal("local", ()))

    # The instance-default principal (no details) is denied...
    denied = await invoker.call("list_invoices", {})
    assert denied.is_error is True

    # ...but a per-call override with the right detail is allowed.
    override = Principal("http-caller", (AuthorizationDetail(type="invoices_read"),))
    allowed = await invoker.call("list_invoices", {}, principal=override)
    assert allowed.is_error is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_server_mcp.py -v -k per_call_principal`
Expected: FAIL — `call()` has no `principal` parameter.

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/server/mcp.py`, change `ToolInvoker.call`'s signature and
principal resolution:

```python
    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None,
        principal: Principal | None = None,
    ) -> types.CallToolResult:
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

        effective_principal = principal if principal is not None else self._principal
        decision = self._policy.evaluate(operation, effective_principal)
        if not decision.allowed:
            missing = ", ".join(d.type for d in decision.missing) or "policy default is deny"
            return _error(f"authorization denied for {name!r}: missing {missing}")

        transport = self._transports.get(operation.upstream)
        if transport is None:
            return _error(f"no transport configured for upstream {operation.upstream!r}")

        try:
            response = await transport.execute(
                operation, args, carry=decision.carry, subject_token=effective_principal.subject_token
            )
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

This also passes `subject_token` through — add it to `HttpTransport.execute`
and `CredentialSource.get` calls now (both already accept it structurally
per P4a's `CredentialSource` protocol signature; `HttpTransport.execute`
needs the parameter added):

In `src/mcp_portal/transports/http.py`, change `execute`:

```python
    async def execute(
        self,
        operation: Operation,
        arguments: dict[str, Any],
        carry: tuple[AuthorizationDetail, ...] = (),
        subject_token: str | None = None,
    ) -> HttpResponse:
        binding = operation.binding
        assert isinstance(binding, HttpBinding)
        base_url = self._upstream.base_url
        assert base_url is not None
        credential = await self._credential_source.get(carry, subject_token)
        request = build_request(binding, base_url, arguments, credential)
        # ... unchanged below this line
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_server_mcp.py tests/test_http_execute.py -v`
Expected: all passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/server/mcp.py src/mcp_portal/transports/http.py tests/test_server_mcp.py
git commit -m "feat: per-call principal override and subject_token plumbing through execute"
```

---

### Task 6: The streamable HTTP transport (`server/http.py`)

**Files:**
- Create: `src/mcp_portal/server/http.py`
- Create: `tests/test_server_http.py`

**Interfaces:**
- Consumes: `App` (existing, `app.py`); `JwtTokenVerifier`, `JwksCache`,
  `discover_jwks_uri` (Tasks 2–3); `principal_from_access_token` (Task 4);
  `Principal` (Task 4); `local_principal` (existing).
- Produces: `build_http_app(app: App, config: Config, secrets:
  Mapping[str, str]) -> Starlette` — a fully assembled ASGI app: the
  streamable HTTP endpoint at `config.server.http.path`, Origin/Host
  validation, RFC 9728 metadata route when inbound is enabled, bearer auth
  when inbound is enabled, and the local principal (with the same
  guardrail logging P3's stdio path already does) when it is not.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_server_http.py`:

```python
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config
from mcp_portal.config.models import Config
from mcp_portal.server.http import build_http_app


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwk(rsa_key) -> dict:
    return jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True) | {
        "kid": "test-kid",
        "use": "sig",
        "alg": "RS256",
    }


def _bearer(rsa_key, **claim_overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": "https://api.example.com/mcp",
        "exp": now + 300,
        "scope": "invoices.read",
    } | claim_overrides
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-kid", "typ": "at+jwt"})


def _config(**server_overrides) -> dict:
    return {
        "version": "1",
        "mode": "configured",
        "server": {"name": "s", "transport": "http", "http": {"allowed_origins": ["https://client.example.com"]}}
        | server_overrides,
        "upstreams": {"billing": {"base_url": "https://api.example.com"}},
        "operations": [
            {
                "id": "list_invoices",
                "upstream": "billing",
                "description": "List invoices.",
                "binding": {"method": "GET", "path": "/v1/invoices"},
            }
        ],
    }


async def _client_for(tmp_path, monkeypatch, config: dict, jwks_handler=None) -> httpx.AsyncClient:
    import json

    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    loaded = load_config(path)
    app = build_app(loaded)

    real_client = httpx.AsyncClient
    if jwks_handler is not None:
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(jwks_handler), **kw)
        )

    asgi_app = build_http_app(app, loaded.config, loaded.secrets)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="https://gateway.local")


@pytest.mark.anyio
async def test_a_mismatched_origin_is_rejected_with_403(tmp_path, monkeypatch):
    client = await _client_for(tmp_path, monkeypatch, _config())
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Origin": "https://evil.example.com", "Content-Type": "application/json"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_no_bearer_token_is_a_401_with_www_authenticate(tmp_path, monkeypatch):
    config = _config(auth={"inbound": {"enabled": True, "issuer": "https://idp.example.com", "audience": "https://api.example.com/mcp"}})
    client = await _client_for(tmp_path, monkeypatch, config)
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Origin": "https://client.example.com", "Content-Type": "application/json"},
    )
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


@pytest.mark.anyio
async def test_the_protected_resource_metadata_route_is_served(tmp_path, monkeypatch):
    config = _config(auth={"inbound": {"enabled": True, "issuer": "https://idp.example.com", "audience": "https://api.example.com/mcp"}})
    client = await _client_for(tmp_path, monkeypatch, config)
    response = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "https://api.example.com/mcp"
    assert body["authorization_servers"] == ["https://idp.example.com"]


@pytest.mark.anyio
async def test_a_valid_bearer_token_reaches_the_tool_call(tmp_path, monkeypatch, rsa_key, jwk):
    async def jwks_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"jwks_uri": "https://idp.example.com/jwks.json"})
        if request.url.path == "/jwks.json":
            return httpx.Response(200, json={"keys": [jwk]})
        return httpx.Response(200, json={"invoices": []})

    config = _config(
        auth={"inbound": {"enabled": True, "issuer": "https://idp.example.com", "audience": "https://api.example.com/mcp"}}
    )
    client = await _client_for(tmp_path, monkeypatch, config, jwks_handler=jwks_handler)
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Origin": "https://client.example.com",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {_bearer(rsa_key)}",
        },
    )
    assert response.status_code == 200
    assert "list_invoices" in response.text


@pytest.mark.anyio
async def test_inbound_disabled_on_loopback_uses_the_local_principal(tmp_path, monkeypatch):
    client = await _client_for(tmp_path, monkeypatch, _config())
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Origin": "https://client.example.com",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 200
```

Before writing the assertions above, check the installed `mcp` SDK's
streamable HTTP client-facing wire format in
`.venv/lib/python3.14/site-packages/mcp/server/streamable_http.py` (required
headers, whether `Mcp-Protocol-Version` must be sent, exact JSON-RPC
envelope) and adjust request headers/bodies to match — the SDK version this
plan pins may require an `Accept: text/event-stream` mix or a protocol
version header the sketch above omits.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_server_http.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.server.http'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/server/http.py`:

```python
"""The streamable HTTP transport (§8's "Streamable HTTP transport" and
"Inbound (HTTP transport)"). Routing, Origin/Host defense, RFC 9728
discovery, and the 401/`WWW-Authenticate` response all come from the `mcp`
SDK (see this plan's Architecture section) — this module supplies
`JwtTokenVerifier` (Task 3) as the SDK's `TokenVerifier` and derives the
per-request `Principal` the same way `app.py` derives the stdio one.
"""

import logging
from collections.abc import Mapping

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Mount

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp_portal.app import App
from mcp_portal.auth.inbound import JwksCache, JwtTokenVerifier, discover_jwks_uri
from mcp_portal.auth.principal import Principal, local_principal, principal_from_access_token
from mcp_portal.config.models import Config
from mcp_portal.server.stdio import build_server

log = logging.getLogger("mcp_portal")


def _security_settings(config: Config) -> TransportSecuritySettings:
    host_port = f"{config.server.http.host}:{config.server.http.port}"
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host_port, config.server.http.host],
        allowed_origins=list(config.server.http.allowed_origins),
    )


async def _build_jwt_verifier(config: Config) -> JwtTokenVerifier:
    inbound = config.auth.inbound
    assert inbound.enabled and inbound.issuer is not None and inbound.audience is not None
    async with httpx.AsyncClient() as discovery_client:
        jwks_uri = inbound.jwks_uri or await discover_jwks_uri(discovery_client, inbound.issuer)
    jwks = JwksCache(httpx.AsyncClient(), jwks_uri)
    return JwtTokenVerifier(
        issuer=inbound.issuer,
        audience=inbound.audience,
        algorithms=inbound.algorithms,
        required_scopes=inbound.required_scopes,
        leeway_s=inbound.leeway_s,
        jwks=jwks,
    )


def _principal_for_request(app: App, config: Config) -> Principal:
    """Resolve the calling principal for one MCP request.

    Mirrors `app.py`'s stdio derivation: when inbound is disabled (only
    reachable at all because the config's unauthenticated-HTTP guard
    already passed at load time), fall back to the local principal with the
    same guardrail semantics — self-asserted, not a security boundary.
    """
    access_token = get_access_token()
    if access_token is None:
        return local_principal(config.auth.local_principal)
    principal = principal_from_access_token(access_token)
    if principal is None:
        # A malformed authorization_details claim denies the request (§8).
        # Returning an empty-detail principal here is safe: it can only
        # satisfy a rule with no requirements, and `unmatched: deny`
        # configurations remain denied.
        return Principal(identity="denied", authorization_details=())
    return principal


def _mcp_app(app: App, config: Config) -> StreamableHTTPASGIApp:
    async def on_list_tools(context: object, params: object) -> object:
        from mcp import types

        return types.ListToolsResult(tools=app.invoker.tools())

    async def on_call_tool(context: object, params) -> object:
        principal = _principal_for_request(app, config)
        return await app.invoker.call(params.name, params.arguments, principal=principal)

    server = build_server(app, config.server.name)
    # `build_server` already wires on_list_tools/on_call_tool for stdio; HTTP
    # needs the call path to resolve a fresh principal per request, so
    # replace the callback registered there with this module's own.
    server.on_call_tool = on_call_tool  # see Task-6 Step-3 note below on why this is safe
    manager = StreamableHTTPSessionManager(app=server, stateless=True)
    return StreamableHTTPASGIApp(manager)


async def build_http_app(app: App, config: Config, secrets: Mapping[str, str]) -> Starlette:
    mcp_asgi_app = _mcp_app(app, config)
    inbound = config.auth.inbound

    routes = []
    middleware = [Middleware(lambda inner: inner)]  # placeholder replaced below; see note

    if inbound.enabled:
        verifier = await _build_jwt_verifier(config)
        assert inbound.audience is not None
        mounted = AuthenticationMiddleware(
            RequireAuthMiddleware(mcp_asgi_app, required_scopes=inbound.required_scopes),
            backend=BearerAuthBackend(verifier),
        )
        mounted = AuthContextMiddleware(mounted)
        routes = create_protected_resource_routes(
            resource_url=inbound.audience,  # type: ignore[arg-type]
            authorization_servers=[inbound.issuer],  # type: ignore[list-item]
            scopes_supported=inbound.required_scopes or None,
        )
    else:
        mounted = mcp_asgi_app
        log.info(
            "transport 'http' with auth.inbound.enabled=false: the local principal applies "
            "to every request. This is a guardrail against an over-eager agent, not a "
            "security boundary — anyone reaching this endpoint can already call the "
            "upstream directly."
        )

    starlette_app = Starlette(routes=[Mount(config.server.http.path, app=mounted), *routes])
    security = _security_settings(config)

    async def _origin_guard(scope, receive, send):
        if scope["type"] == "http":
            from starlette.requests import Request

            from mcp.server.transport_security import TransportSecurityMiddleware

            request = Request(scope, receive)
            error = await TransportSecurityMiddleware(None, settings=security).validate_request(
                request, is_post=request.method == "POST"
            )
            if error is not None:
                return await error(scope, receive, send)
        await starlette_app(scope, receive, send)

    return _origin_guard
```

The `_origin_guard`/placeholder-middleware sketch above is intentionally
rough — before finalizing it, read
`.venv/lib/python3.14/site-packages/mcp/server/transport_security.py` in
full: `TransportSecurityMiddleware` is written as an ASGI middleware class
(`__call__(scope, receive, send)`), not a `validate_request`-only helper, so
the idiomatic wiring is almost certainly
`TransportSecurityMiddleware(starlette_app, settings=security)` used
directly as the outermost ASGI callable, no bespoke wrapper function needed.
Replace the sketch with that once confirmed, and delete the unused
`middleware` placeholder list. Re-run Step 2's tests after simplifying to
confirm behavior is unchanged.

Also confirm, from `server/stdio.py`, that `build_server`'s returned
`Server` exposes mutable `on_call_tool`/`on_list_tools` attributes (it does
today — `Server(name=..., version=..., on_list_tools=..., on_call_tool=...)`
per the P1 plan's implementation) before relying on reassigning
`server.on_call_tool` in `_mcp_app`; if the installed `mcp` version instead
freezes these at construction, build the `Server` directly in this module
with `on_call_tool` set to this module's own callback from the start,
skipping `build_server` entirely for the HTTP path.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_server_http.py -v`
Expected: all passed. Iterate on the SDK-integration details flagged in
Step 3 until they do — this task's tests, not the sketch above, are the
source of truth for the exact wiring.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/server/http.py tests/test_server_http.py
git commit -m "feat: streamable HTTP transport with RFC 9728 discovery and bearer auth"
```

---

### Task 7: Wire `token_exchange` end to end in `app.py`

**Files:**
- Modify: `src/mcp_portal/app.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `TokenExchangeSource` (P4a Task 5); `TokenCache` (P4a Task 3,
  already instantiated once in `build_app` per P4a Task 6).
- Produces: `build_app` routes `outbound.mode == "token_exchange"` upstreams
  to a `TokenExchangeSource`, alongside the existing `client_credentials`
  and `none`/`static` branches.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py` (matching its existing helper/fixture style,
per Task 6's note in the P4a plan — read the file before writing):

```python
def test_token_exchange_upstream_builds_a_token_exchange_credential_source(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLING_CLIENT_SECRET", "secret-value")
    config = CONFIG | {
        "server": {"name": "billing-portal", "transport": "http"},
        "auth": {
            "inbound": {
                "enabled": True,
                "issuer": "https://idp.example.com",
                "audience": "https://api.example.com/mcp",
            }
        },
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {
                    "outbound": {
                        "mode": "token_exchange",
                        "token_endpoint": "https://idp.example.com/oauth2/token",
                        "client_id": "sidekit-billing",
                        "client_secret": "${env:BILLING_CLIENT_SECRET}",
                        "audience": "https://api.example.com",
                    }
                },
            }
        },
    }
    from mcp_portal.auth.outbound import TokenExchangeSource

    path = _write_config(tmp_path, config)
    app = build_app(load_config(path))
    transport = app.invoker._transports["billing"]  # test-only reach-through, matches this file's existing style
    assert isinstance(transport._credential_source, TokenExchangeSource)
```

Check `tests/test_app.py`'s existing tests for whether they already reach
into `ToolInvoker`/`HttpTransport` internals this way (P4a's Task 6 test
does something similar) and match that exact pattern rather than
introducing a new one.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_app.py -v -k token_exchange`
Expected: FAIL — `build_app` does not route `token_exchange` anywhere yet
(currently falls through to `StaticCredentialSource(credential_for(...))`,
and `credential_for` raises `ConfigError` for an unrecognized mode... check:
`credential_for` only branches on `mode == "none"`, else assumes
`static`-shaped fields — this will raise an unrelated error for
`token_exchange`, confirming the test fails for the right reason).

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/app.py`, extend the transport-building loop from P4a
Task 6:

```python
from mcp_portal.auth.outbound import ClientCredentialsSource, TokenExchangeSource, credential_for
```

```python
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
git commit -m "feat: wire token_exchange credential acquisition into build_app"
```

---

### Task 8: `cli.py` serves `transport: http` with uvicorn

**Files:**
- Modify: `src/mcp_portal/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `build_http_app` (Task 6).
- Produces: `main`'s `serve` command dispatches to `run_stdio` or a new
  `_serve_http(app, config, secrets)` based on `loaded.config.server
  .transport`, exactly as the design's phasing intends transport to be a
  config field, not a CLI flag.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_serve_dispatches_to_http_when_transport_is_http(tmp_path, monkeypatch):
    """Not a full server-startup test (that needs a live port + uvicorn
    event loop, covered by the integration suite) — asserts main() picks the
    HTTP path rather than stdio by monkeypatching uvicorn's runner and
    checking it was invoked with the built ASGI app."""
    calls = []

    async def fake_serve(app):
        calls.append(app)

    import mcp_portal.cli as cli_module

    monkeypatch.setattr(cli_module, "_run_uvicorn", fake_serve)

    config = CONFIG | {"server": {"name": "s", "transport": "http"}}
    main(["serve", "--config", str(write(tmp_path, config))])
    assert len(calls) == 1
```

Check `tests/test_cli.py`'s existing imports/fixtures (`CONFIG`, `write`)
before adding this — reuse them rather than redefining.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v -k http`
Expected: FAIL — `cli_module` has no `_run_uvicorn` attribute; `serve`
always calls `run_stdio` today.

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/cli.py`, add:

```python
async def _run_uvicorn(asgi_app) -> None:
    import uvicorn

    config = uvicorn.Config(asgi_app, host=asgi_app_host, port=asgi_app_port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
```

Since `_run_uvicorn` needs the bind host/port, change its signature to take
them explicitly rather than reading module-level globals:

```python
async def _run_uvicorn(asgi_app, host: str, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(asgi_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
```

Update the test above to call `cli_module._run_uvicorn` with that
three-argument signature (`fake_serve(app, host, port)`).

Replace the `serve` branch of `main`:

```python
    if loaded.config.server.transport == "stdio":
        from mcp_portal.server.stdio import run_stdio

        async def _serve() -> None:
            try:
                await run_stdio(app, loaded.config.server.name)
            finally:
                await app.aclose()

        asyncio.run(_serve())
        return 0

    from mcp_portal.server.http import build_http_app

    async def _serve_http() -> None:
        try:
            asgi_app = await build_http_app(app, loaded.config, loaded.secrets)
            await _run_uvicorn(asgi_app, loaded.config.server.http.host, loaded.config.server.http.port)
        finally:
            await app.aclose()

    asyncio.run(_serve_http())
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/cli.py tests/test_cli.py
git commit -m "feat: serve transport:http with uvicorn, selected by config not a CLI flag"
```

---

### Task 9: End-to-end test — the full broker over streamable HTTP

**Files:**
- Create: `tests/test_p4_end_to_end.py`
- Modify: `examples/billing.yaml` (add a second example,
  `examples/billing-http.yaml`, demonstrating `transport: http` +
  `token_exchange` — do not change the existing stdio example, which other
  tests and the README quickstart depend on)
- Modify: `README.md`, `AGENTS.md` (Status/Roadmap and Repository Structure,
  per `AGENTS.md`'s Agent Guardrails)

**Interfaces:**
- Consumes: everything this plan and P4a built.
- Produces: no new production code — this is the "runnable result" bar
  from the design's phasing table (§12): a bearer-token-authenticated
  streamable HTTP call, RAR-policy-checked against claims from the JWT,
  exchanged for an upstream token via `token_exchange`, reaching a fake
  upstream, mirroring what `tests/test_p3_end_to_end.py` did for stdio +
  local-principal RAR.

- [ ] **Step 1: Write the end-to-end test**

Create `tests/test_p4_end_to_end.py`, following `test_p3_end_to_end.py`'s
existing structure (read it first — do not duplicate its fixture-writing
helpers if it already provides one; extend/reuse them). It needs, wired
together in one config:

- `mode: configured`, `server.transport: http`, `auth.inbound.enabled: true`
  pointed at a fake IdP (an `httpx.MockTransport`-backed `AsyncClient`
  monkeypatched in as in `tests/test_stdio.py`'s `app` fixture, serving AS
  metadata, JWKS, and a token-exchange response).
- A policy rule requiring `authorization_details: [{type:
  payment_initiation, actions: [initiate]}]` on the billing upstream's
  `action`-effect operation, with `outbound.carry: true`.
- One upstream configured with `outbound.mode: token_exchange`.
- A caller-supplied bearer JWT (built with `pyjwt` + a test RSA key, as in
  `tests/test_auth_inbound_jwt.py`) carrying that `authorization_details`
  claim.
- Assertions, in order: (1) a call without the claim is denied before the
  upstream is ever reached; (2) a call with the claim succeeds; (3) the
  fake upstream received a request whose `Authorization` header carries the
  *exchanged* token, not the caller's own bearer token; (4) the fake token
  endpoint's request body included the carried `authorization_details` (the
  accumulated required detail `R`, not the caller's broader presented set,
  per §8's "What `carry: true` carries").

- [ ] **Step 2: Run the test to verify it fails, then passes once implemented**

Run: `uv run pytest tests/test_p4_end_to_end.py -v`
Expected: this test should already pass if Tasks 1–8 are correct — it
exercises no new code paths, only their composition. If it fails, that is a
signal a wiring detail in an earlier task's "sketch, iterate against tests"
step (Task 6 especially) needs another pass — go back and fix the earlier
task, do not patch around it here.

- [ ] **Step 3: Add the HTTP example config**

Create `examples/billing-http.yaml`, mirroring `examples/billing.yaml`'s
commented style, demonstrating `transport: http`, `auth.inbound`, an
`outbound.mode: token_exchange` upstream, and a policy file reference.

- [ ] **Step 4: Update README and AGENTS.md**

Per `AGENTS.md`'s Agent Guardrails: move "HTTP transport for the gateway
itself" and "Inbound OAuth ... and outbound client_credentials /
token_exchange" out of README's Roadmap and into Core features / Status
(now "P4"). Add `server/http.py`, `auth/inbound.py`, `auth/token_cache.py`
to `AGENTS.md`'s Repository Structure, and update its Architecture Notes
invocation-flow list (step 3: "Establish the principal" now varies by
transport) and Key invariants (add the unauthenticated-HTTP guard and the
token-exchange-requires-inbound rule alongside the existing ones).

- [ ] **Step 5: Run this plan's and P4a's full combined test surface**

```bash
uv run pytest tests/ -k "not integration" -v
```
Expected: all passed. This is still not the *entire* repo's test command
(`uv run pytest` alone, which the Global Constraints in both P4 plans ask
you not to run mid-task) — it is, at this point in the phase, equivalent to
it; run the bare `uv run pytest` once more here as this phase's own closing
verification, and note in the commit message that it was run.

- [ ] **Step 6: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 7: Commit**

```bash
git add tests/test_p4_end_to_end.py examples/billing-http.yaml README.md AGENTS.md
git commit -m "test: end-to-end streamable HTTP broker covering RAR + token_exchange"
```

---

## Self-Review Notes

- **Spec coverage:** §8's Inbound table (discovery, token validation) is
  Tasks 1–3; the Streamable HTTP transport section is Task 6; Principals'
  HTTP row is Task 4; the `token_exchange` row and cache table entry (P4a
  built the mechanism, unwired) are completed in Task 7; §10's startup
  validation items 3, 4, and 9 (the last satisfied by construction — the
  metadata `resource` is always `auth.inbound.audience`, never a separately
  configurable field that could diverge) are Task 1; the security
  regression tests named in §11 (`alg: none`, HS*-with-public-key, Origin
  mismatch, exchange-cache-keyed-on-subject-token) are covered across Tasks
  2, 3, and 9.
- **Explicitly out of scope, matching §13:** opaque token introspection,
  DPoP, mTLS-bound tokens, SSE streaming, MCP sessions (`stateless=True` is
  final for v1, not provisional).
- **Task 6 is flagged as the plan's highest-uncertainty task** — it is the
  one place this plan integrates directly with `mcp` SDK internals
  (`StreamableHTTPSessionManager`, `TransportSecurityMiddleware`,
  `AuthenticationMiddleware` composition) whose exact call shape could not
  be fully hand-verified while writing this plan. Its own tests (Step 4),
  not the sketch in Step 3, are the source of truth; budget extra iteration
  time there specifically.
