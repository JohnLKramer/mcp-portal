# mcp-portal: MCP Sidecar for HTTP Services — Design

**Date:** 2026-09-19
**Status:** Approved for implementation planning (revised after review)
**Scope of this spec:** Core runtime, HTTP/OpenAPI support, and the complete
configuration contract including OAuth and Rich Authorization Requests.
**Review:** [`2026-09-19-mcp-sidekit-design-review.md`](./2026-09-19-mcp-sidekit-design-review.md)

---

## 1. Summary

`mcp-portal` is a sidecar server that exposes an existing API as MCP tools. It
points at an upstream service, learns what that service offers — either from an
explicit config file or by introspecting an OpenAPI document — and serves the
result to MCP clients over stdio or streamable HTTP.

It also brokers authorization. Inbound, it acts as an OAuth 2.0 resource server,
validating caller tokens. Outbound, it acts as an OAuth 2.0 client, acquiring or
exchanging tokens for the upstream. Between the two sits a policy engine built on
OAuth 2.0 Rich Authorization Requests (RFC 9396), so an individual tool can
declare the authority it requires rather than relying on a coarse scope.

### Goals

- Turn an HTTP API into MCP tools with no hand-written glue in the common case.
- Publish a versioned, machine-checkable configuration contract.
- Make the exposed surface deliberate: risky operations should require a decision,
  not an oversight.
- Keep authorization policy separate from, and durable against, regenerated
  API descriptions.
- Leave clean seams for gRPC and GraphQL without building them speculatively.

### Non-goals for this spec

- gRPC and GraphQL transports. The seams are designed here; the implementations
  are separate slices with their own specs.
- DASH, HLS, and QUIC. Noted as long-term interest, not designed for.
- Any MCP surface other than tools. Resources and prompts are out of scope.

---

## 2. Operating modes

Mode is a top-level, explicitly named selector. It controls **which operations
become callable tools**. It never controls authorization.

| Mode | Behavior |
|---|---|
| `introspect-unsafe` | Introspect the upstream and expose everything discovered, including operations that change state. |
| `introspect-safe` | Introspect and expose only operations that are `read_only` and not `sensitive`. |
| `configured` | No introspection. Serves exactly the operations written in config. |

`configure` is **not** a mode. It is a CLI subcommand (`mcp-portal configure`)
that introspects, surveys, writes files, and exits without serving. The schema's
`mode` enum contains exactly the three values above.

### How `introspect-unsafe` is kept out of reach

Stated mechanically, because "cannot be reached by omission" is otherwise an
aspiration rather than a property:

- `mode` is **required** in the config schema and has **no default**. A config
  omitting it fails to load.
- The CLI has no default `--mode`. There is no invocation that selects a mode
  implicitly.
- `introspect-unsafe` additionally requires `acknowledge_unsafe: true` in config.
  Selecting the mode alone is insufficient.
- On startup it logs a banner enumerating every state-changing tool it exposed,
  so the blast radius appears in the terminal and in logs.

### The governing principle

**Mode controls exposure. Policy controls authorization.**

These are different questions and the design keeps them apart everywhere.
`introspect-unsafe` means "discover and surface everything." It never means "skip
the policy checks." Policy is evaluated on every tool invocation in every mode,
including stdio.

---

## 3. Architecture

A one-way pipeline. Every mode is a different way of entering it.

```
sources → classify → select → name → ToolSet → server → invoke → auth → transport → result
```

**Naming runs after selection**, and the order is load-bearing. Names carry
collision-resolution suffixes derived from the surviving set, so naming before
selection would let removing one operation silently rename another.

### Modules

Under `src/mcp_portal/`:

| Module | Responsibility |
|---|---|
| `operations.py` | The `Operation` domain model, `Effect`, `Sensitivity`, and the discriminated `Binding` union. Pure data, no I/O. |
| `config/models.py` | Pydantic models for the main config. |
| `config/policy.py` | Pydantic models for the RAR policy file. |
| `config/loader.py` | File loading (JSON and YAML), `${env:}` / `${file:}` resolution, validation, startup checks (§10). |
| `config/schema.py` | JSON Schema emission and the drift check. |
| `sources/base.py` | The `OperationSource` protocol: something that yields `Operation`s. |
| `sources/explicit.py` | Config `operations[]` entries → `Operation`. |
| `sources/openapi.py` | OpenAPI document → `Operation`, including dialect conversion. |
| `classify.py` | Method-derived effect classification, plus `classification[]` overrides. |
| `registry.py` | Selection rules, mode posture, explicit/introspected merge. |
| `naming.py` | Tool-name generation, namespacing, collision resolution. Runs on the selected set. |
| `transports/base.py` | The `TransportAdapter` protocol. |
| `transports/http.py` | `build_request()` (pure) and execution of an `HttpBinding`. |
| `auth/inbound.py` | Resource server: JWKS cache, JWT validation, principal construction. |
| `auth/outbound.py` | OAuth client: static, client credentials, token exchange, token cache. |
| `auth/rar.py` | `authorization_details` parsing and the coverage predicate. |
| `policy.py` | Rule matching and accumulation, then the allow/deny decision. Calls `auth/rar.py` per requirement. |
| `server/mcp.py` | Tool registration, MCP annotations, the invoke handler. |
| `server/stdio.py`, `server/http.py` | The two transport frontends. |
| `configure/survey.py` | The interactive survey. |
| `configure/reconcile.py` | Diffing an existing config against a fresh introspection. |
| `cli.py` | Entrypoint and subcommand dispatch. |

### Why these boundaries

**Sources produce operations; they never decide exposure.** A source answers only
"what does this upstream offer." Whether an operation becomes a callable tool is
`registry.py`'s decision. Mode is an input to the registry and touches nothing
else, which is what stops three modes from becoming three introspection code paths.

**`auth/` never knows which protocol it is authorizing.** It yields a credential.
`transports/http.py` receives a ready credential and never reasons about OAuth.
This seam is why the gRPC slice inherits the broker unchanged — and the broker is
the most expensive component in this build.

**`operations.py` performs no I/O.** Classification, selection, naming, request
construction, and RAR coverage are pure functions over constructed `Operation`s,
so the subtle logic is testable without a network, a token, or a running server.

**The server never imports `configure/`.** Authoring and serving share data, not
code paths. A defect in the survey cannot reach a running server.

### Where generality stops

`Binding` is a closed union discriminated on `protocol`. Only `HttpBinding` exists
today. There is deliberately no attempt to unify HTTP's path/method/parameter-location
model with a future gRPC service/method model — that unification is where this
design would over-fit. Cross-cutting concerns hang off `Operation`; protocol
mechanics stay sealed inside the binding.

---

## 4. The operation model

```python
@dataclass(frozen=True)
class Operation:
    id: str  # stable identity; the primary key everywhere
    upstream: str  # key into config.upstreams
    name: str  # MCP tool name; derived, never a match key
    title: str  # human-readable MCP title
    description: str
    group_tags: tuple[str, ...]
    effect: Effect  # read_only | idempotent_write | action
    sensitivity: Sensitivity  # normal | sensitive
    input_schema: dict  # JSON Schema 2020-12, object type, self-contained
    binding: Binding  # HttpBinding today
```

Stable identity is `operationId` when the upstream provides one, otherwise a slug
of method and path. Reconcile depends on this being stable across introspections;
a change of identity reads as "one operation removed, one added," which is the
correct and visible outcome.

**`id` is the primary key for selection, classification overrides, and policy
matching.** `name` is a derived presentation value and is never matched against —
see §5.

### Risk: two axes, not one

Risk is expressed as two independent properties, because a single enum cannot
represent the case that motivates the whole idea.

**`effect`** — always present, derived from the HTTP method per RFC 9110:

| Method | Effect | MCP annotations |
|---|---|---|
| GET | `read_only` | `readOnlyHint: true`, `idempotentHint: true`, `destructiveHint: false` |
| PUT, DELETE | `idempotent_write` | `readOnlyHint: false`, `idempotentHint: true`, `destructiveHint: true` |
| POST, PATCH | `action` | `readOnlyHint: false`, `idempotentHint: false`, `destructiveHint: true` |

`openWorldHint` is `true` for every operation: sidekit calls an external system
whose state it does not control. `title` comes from OpenAPI `summary` when
present, otherwise the generated tool name.

**HEAD and OPTIONS are never exposed as tools.** They carry no response body a
model can use and exist for cache and CORS negotiation. They are dropped at the
source, in every mode.

**`sensitivity`** — `normal` or `sensitive`, defaulting to `normal`. Never derived.

`GET /users/{id}/ssn` is unambiguously read-only and just as unambiguously not
safe to expose. One enum cannot say that; two axes can. This also gives
`introspect-safe` a precise definition: expose operations that are `read_only`
**and** not `sensitive`.

**Where sensitivity comes from.** Three sources, all evaluated at registry-build
time: an `x-mcp-sensitive` OpenAPI extension, an explicit `operations[]` entry, or
a `classification[]` rule in the main config (below). It is **not** settable from
the policy file — policy is evaluated at invocation, long after the `ToolSet`
exists, and a policy-set sensitivity would also make `match.sensitivity` circular.

**Stated limitation.** Sensitivity cannot be introspected. `introspect-safe`
therefore protects against method-level mistakes, not against an upstream that
serves secrets over GET. Documentation must say this in these words; the mode name
alone over-promises.

### Classification overrides

In the main config, applied after method-derived defaults and before selection:

```yaml
classification:
  - match: { ids: ["get_user_ssn", "export_*"] }
    sensitivity: sensitive
  - match: { tags: [admin] }
    sensitivity: sensitive
  - match: { ids: ["reindex_search"] }
    effect: action          # upstream models it as PUT but it is not idempotent
```

Rules apply in order; later rules win on conflict. `match` accepts `ids` (globs
over `id`), `tags`, `upstream`, and `effect`.

### Tags

Two separate namespaces, matched independently:

- **Group tags** come from OpenAPI `tags` and are organizational (`billing`,
  `admin`). They drive selection, naming, and policy matching.
- **Effect and sensitivity** are the risk classification. They drive MCP
  annotations, exposure posture, retry behavior, and policy matching.

These are kept distinct so an API that happens to have a tag named `admin` does
not acquire risk semantics by accident.

### Tool naming

Configurable strategy, `operation_id` (default) or `method_path`, optionally
prefixed with upstream key and/or group tag.

- Generated names match `^[a-z0-9_]{1,64}$` — lowercase, `_` separators. **64, not
  128**: several MCP clients cap tool names at 64 characters, and upstream and tag
  prefixes plus a collision suffix consume budget quickly. The cap is fixed, not
  configurable.
- An explicit `name` in an `operations[]` entry is validated against the same
  regex and is not transformed.
- `prefix_with_group_tag` uses the **first tag in document order**. An operation
  with no tags gets no prefix.
- Collisions append `_` plus the first 6 hex characters of `sha256(id)`. A
  collision that survives suffixing is a load error, not a silent rename.
- Names exceeding the cap are truncated to 57 characters and hash-suffixed.

Because names change with strategy, prefixes, truncation, and collisions, they are
**presentation only**. Nothing matches on them.

---

## 5. The configuration contract

Two files, two published JSON Schemas. Both schemas are **generated from the
Pydantic models** and committed under `schema/`. CI fails when the committed
schema does not match what the models produce. A hand-maintained schema drifts
from the code that reads it, and a drifted schema on a public contract is worse
than no schema.

Both JSON and YAML are accepted and validated against the same schema. YAML
permits comments, which policy files benefit from.

`version` is the version of the **contract**, not of the application. A `version`
the binary does not recognize is a **load error**, never a best-effort parse. A
migration path is provided per contract version bump; `version: "1"` is the only
value accepted today.

**Relative paths** — `policy.file` and `${file:...}` references resolve relative
to the **directory containing the config file**, not the process working
directory, so a config behaves identically regardless of where it is launched from.
Secret values read via `${file:}` have a single trailing newline stripped.

### Main config

```yaml
$schema: https://schemas.mcp-portal.dev/config-v1.schema.json
version: "1"
mode: configured

server:
  name: billing-portal
  transport: stdio            # stdio | http
  http:
    host: 127.0.0.1
    port: 8080
    path: /mcp
    allowed_origins: ["http://localhost:*"]

upstreams:
  billing:
    protocol: http
    base_url: https://api.example.com
    timeout_ms: 30000
    introspection:
      openapi: { url: https://api.example.com/openapi.json }
    auth:
      outbound:
        mode: client_credentials
        token_endpoint: https://idp.example.com/oauth2/token
        client_id: sidekit-billing
        client_secret: ${env:BILLING_CLIENT_SECRET}
        scopes: [invoices.read]

auth:
  inbound:
    enabled: true
    issuer: https://idp.example.com
    audience: https://sidekit.example.com/mcp
    required_scopes: [mcp.invoke]
  local_principal:
    authorization_details: []

classification: []
selection:
  include_tags: [billing]
  exclude_tags: [internal]
  exclude_ids: ["*_internal"]
naming: { strategy: operation_id, prefix_with_group_tag: true }
policy: { file: ./rar-policy.yaml }
operations: []
```

Multiple upstreams are supported. Each carries its own `base_url`, introspection
source, and — importantly — its own outbound credentials, since different services
mean different clients. Inbound auth is global because it concerns the caller, not
the callee. Upstream key is available as a naming prefix and as a policy match key.

### Selection

Applied after the mode's posture and after classification, before naming:

| Field | Meaning |
|---|---|
| `include_tags` | Group tags to include, **any-match**: an operation is included if it carries at least one listed tag. Omitted means all. |
| `exclude_tags` | Group tags to exclude, any-match. |
| `include_ids` | Globs over `id`. Omitted means all. |
| `exclude_ids` | Globs over `id`. |

Exclusions always win over inclusions. **There is no name-based selection.** Names
are unstable by construction (§4), so a name glob that stops matching after a
collision suffix appears would fail open — silently exposing something the
operator excluded.

A selection glob or tag that matches zero operations logs a **warning** at startup
naming the dead rule. It is not an error: in a multi-upstream config, a rule that
matches nothing on one upstream is legitimate.

### Explicit operation entries

An explicit entry and an introspected operation produce the identical model.

```yaml
operations:
  - id: list_invoices
    upstream: billing
    description: List invoices for a customer.
    group_tags: [billing]
    binding:
      protocol: http
      method: GET
      path: /v1/invoices
      parameters:
        - { arg: customer_id, in: query, wire_name: customerId,
            required: true, style: form, explode: true,
            schema: { type: string } }
        - { arg: invoice_id,  in: path,  wire_name: id,
            required: true, schema: { type: string } }
      body:
        content_type: application/json
        mode: flatten          # flatten | single_arg
        schema: { type: object, properties: { amount: { type: integer } } }
```

Every field the model requires but the entry omits is derived: `effect` from
`binding.method`, `name` from the naming strategy, `sensitivity` as `normal`,
`title` from `description`'s first line. An entry may set `effect`, `name`,
`title`, or `sensitivity` explicitly to override.

`input_schema` is **always synthesized from the binding**, never hand-written —
for explicit entries exactly as for introspected ones, using the flattening rules
in §6. Explicit and introspected operations therefore present identical tool
schemas for identical bindings, and there is one code path to test.

The binding is an explicit `arg → (location, wire_name, style, explode)` mapping.
This is what makes flattening reversible: the tool schema shows `customer_id`, and
the binding knows it goes in the query string as `customerId`.

### Secrets

Fields typed as secrets accept **only** `${env:VAR}` or `${file:/path}`, matching
`^\$\{(env|file):[^}]+\}$`. A literal value fails schema validation — it is a load
error, not a warning. Configs are therefore safe to commit by construction, and a
secret cannot leak into version control through this file by accident.

### RAR policy file

```yaml
version: "1"
defaults:
  unmatched: allow        # or: deny
rules:
  - match:
      tags: [billing]
      effect: [action]
    require:
      authorization_details:
        - type: payment_initiation
          actions: [initiate]
          locations: ["https://api.example.com/v1/payments"]
    outbound:
      carry: true
```

**Why a separate file.** The main config's content is *derived* in introspection
mode — regenerated whenever the upstream document changes. Authorization policy
must never be silently regenerated from a third party's schema, because the
upstream does not get to decide what authority it requires. A separate file means
hand-written policy survives re-introspection by construction. It also has a
different owner and a different review cadence: API plumbing changes weekly, while
authorization policy should stay a small file a reviewer can read end to end.

`match` accepts `ids` (globs over `id`), `tags`, `effect`, `sensitivity`, and
`upstream`. All present criteria must hold. As with selection, **there is no
`match.name`** — a policy rule keyed on an unstable value fails open.

**Matching accumulates.** Every rule whose `match` selects an operation contributes
its requirements; the effective requirement is the union. Rules are never
first-match-wins, under which a broad rule written early shadows a stricter rule
written later, failing open.

**"Matched" means "contributed a requirement."** A rule with an empty or absent
`require` — for instance one written only to set `outbound.carry` — does **not**
make an operation matched for the purposes of `defaults.unmatched`. Without this,
adding a carry-only rule under `unmatched: deny` would flip an operation from
denied to callable-with-no-requirements, which is a loosening. Under `deny`, a
`require`-less rule is additionally a **load error**, so the intent is never
ambiguous.

`defaults.unmatched` is `allow`. Policy is opt-in hardening, so sidekit is usable
before a policy file exists. Setting it to `deny` turns on whitelist mode.

A policy rule matching zero operations logs a **warning** at startup.

### Strict parsing of `require`

The `require.authorization_details[]` model uses **`extra="forbid"`**. An unknown
field is a load error.

This is the single most important parsing decision in the contract. RFC 9396
defines `type`, `actions`, `locations`, `datatypes`, `identifier`, and
`privileges`, and permits type-specific extensions. Under Pydantic's default
behavior, a policy author writing `privileges: [admin]` in a `require` block would
see it parse cleanly and enforce nothing — the rule would be silently broader than
written, in the one file that must fail closed.

Empty arrays are also rejected at load time: `actions: []` means `∅ ⊆ anything`,
which requires nothing while appearing to require something.

---

## 6. Introspection

OpenAPI 3.0 and 3.1, fetched by URL or read from file, with `$ref` resolution.
The document is fetched **once at startup**. There is no runtime refresh: a tool
surface that changes underneath a connected client is a correctness problem, not a
feature. Re-introspection is what restarting, or `configure`, is for.

### Schema dialect conversion

OpenAPI 3.1 schemas *are* JSON Schema 2020-12 and pass through unchanged. OpenAPI
3.0 uses a near-miss dialect — `nullable: true`, boolean `exclusiveMinimum` /
`exclusiveMaximum`, singular `example` — that requires an explicit conversion step.
Passing 3.0 schemas through untouched produces tool schemas that are subtly wrong
in ways that surface only at call time, so conversion is a named, tested step.

### Document handling

| Concern | Behavior |
|---|---|
| `servers[]` vs `base_url` | `base_url` wins when set. Otherwise the first `servers[].url`, resolved against the document URL, with server variables substituted by their declared defaults. Server URLs frequently carry a `/v1` prefix, so getting this precedence wrong silently doubles or drops path segments. |
| `security` / `securitySchemes` | **Ignored entirely.** Outbound auth config is the sole authority on credentials. Honoring the document here would let an upstream document influence which credential sidekit presents. |
| Path-level `parameters` | Merged with operation-level. Operation-level wins on a `(name, in)` collision. |
| `deprecated: true` | Excluded by default. `introspection.include_deprecated: true` to include. |
| External `$ref` | **Disabled by default.** Fetching remote refs from a document is an SSRF vector — the document tells sidekit which URLs to request. Enabled only via `introspection.allow_external_refs` with an explicit host allowlist. |
| Circular `$ref` | Detected. The cycle is replaced with `{"type": "object"}` and a warning is logged, since an MCP `input_schema` must be finite and self-contained. |
| `x-mcp-exclude` | Honored in **every** mode, including `introspect-unsafe`. Exclusion is the upstream's statement that something is not a tool; mode governs risk posture, not upstream opt-outs. An `operations[]` entry may re-include it by declaring it explicitly. |

### Explicit and introspected operations together

`operations[]` entries are merged into introspection modes, not just `configured`.
An explicit entry whose `id` matches an introspected operation **replaces** it;
an entry with a new `id` is added. This is how an operator corrects a bad
description or a wrong `effect` without abandoning introspection.

### `x-mcp-*` extensions

`x-mcp-name`, `x-mcp-title`, `x-mcp-description`, `x-mcp-sensitive`, and
`x-mcp-exclude` are honored when present and are overridable by local config,
which always wins.

### Parameter flattening

Path, query, header, and body parameters are merged into a single flat object
schema. A nested `{path: {...}, query: {...}, body: {...}}` shape is harder for a
model to fill in correctly, and correctness at the tool boundary is the point of
the product.

- Collisions resolve by prefixing the colliding names with their location:
  `query_id`, `path_id`.
- If a prefixed name **re-collides** — a body property literally named `query_id`
  alongside a query parameter `id` — it is a **load error** naming both
  contributors. Silently renaming twice produces an argument name no operator can
  predict.
- Request bodies default to `mode: flatten`, lifting top-level object properties
  into arguments. A non-object body (array, scalar), or `mode: single_arg`,
  becomes one argument named `body`.
- `required` is the union of each contributing source's required set.
- `application/json` is supported in v1. `application/x-www-form-urlencoded` and
  `multipart/form-data` are **deferred**; an operation whose only content type is
  unsupported is skipped with a warning.

---

## 7. `configure`

A CLI subcommand. It introspects, surveys, writes files, and exits. It never
serves MCP.

### Propose, do not interrogate

The survey pre-fills every answer from the derived classification and asks for
confirmation or amendment. It walks group tag by group tag, offering group-level
answers ("expose all 14 read-only operations in `billing`?") before descending to
individual operations.

Without this, a 200-operation API guarantees rubber-stamping — and a survey that
trains its operator to press Enter is worse than no survey, because it manufactures
the appearance of review.

Per operation it confirms exposure, sensitivity, and any policy rule.

### Output

Writes the main config (with operations pinned) and a policy file. Output is
written to a temporary file and presented as a diff for confirmation before
anything existing is overwritten.

### Reconcile

Re-running against an existing config diffs by `id`:

- **New** operations are surveyed.
- **Removed** operations are flagged for deletion, never deleted silently.
- **Changed** input schemas are flagged for re-confirmation.
- **Unchanged** operations are left untouched.

After the delta pass, an opt-in full review walks previously recorded decisions,
for periodic audit.

**Formatting fidelity is best-effort and format-dependent.** YAML configs are
round-tripped with `ruamel.yaml` so comments, key order, and anchors survive. JSON
configs cannot carry comments at all, and key order is preserved but formatting is
normalized. `configure` prints which fidelity applies. Operators who want their
policy comments preserved should use YAML, and the docs should say so.

---

## 8. Authorization

Two distinct concerns that this design deliberately keeps apart:

- **Authentication** establishes who the caller is. HTTP transport only.
- **Policy enforcement** decides whether a call is permitted. Always runs, in
  every mode and on both transports.

### Principals

A principal carries an identity and a set of `authorization_details`.

| Situation | Principal |
|---|---|
| `transport: http`, inbound enabled | Derived from the validated bearer token. |
| `transport: http`, inbound disabled | `auth.local_principal`. Requires the guard below. |
| `transport: stdio` | `auth.local_principal`, defaulting to empty details. |

Policy evaluates identically against all three. One policy file works in
production and locally, unchanged. The alternative — refusing to run policy under
stdio — buys nothing, since anyone who can launch the stdio process can edit the
config or call the upstream directly, and it pushes teams toward maintaining a
second, weaker local policy file that drifts.

**What the local-principal guardrail is honestly for.** The threat model under
stdio is not a malicious operator; it is an over-eager agent. Constraining which
tools a model may invoke is worthwhile even though the human can trivially bypass
it. That is a guardrail, not a security boundary, and the documentation must use
those words. Startup logs which rules are active and that the principal is
self-asserted.

### Unauthenticated HTTP is guarded

`transport: http` with `auth.inbound.enabled: false` is an unauthenticated network
endpoint that holds a service credential and will call the upstream for anyone who
reaches it. That is a **larger** blast radius than `introspect-unsafe`, so it gets
at least equal ceremony:

- It is a **startup error** unless `server.http.host` is a loopback address, or
  `auth.inbound.allow_unauthenticated_http: true` is explicitly present.
- When permitted, the local principal applies, and startup logs the same style of
  banner as `introspect-unsafe`, naming the bind address and every exposed tool.

### Inbound (HTTP transport)

**Discovery and challenge**

- Protected resource metadata per RFC 9728. Because the MCP resource has a path
  (`/mcp`), the metadata lives at the **path-suffixed** location
  `/.well-known/oauth-protected-resource/mcp`.
- The document's `resource` **must equal** `auth.inbound.audience`; a mismatch is
  a startup error. It advertises `authorization_servers` (from `issuer`),
  `scopes_supported` (from `required_scopes`), and
  `bearer_methods_supported: ["header"]`.
- `401` responses carry
  `WWW-Authenticate: Bearer resource_metadata="<url>"`.

**Token validation**

| Control | Behavior |
|---|---|
| Algorithm | Allowlist, defaulting to `RS256`, `ES256`. `none` is rejected unconditionally. HMAC algorithms are rejected unless explicitly allowlisted, since accepting HS\* alongside a public JWKS enables key-confusion. |
| `typ` | `at+jwt` (RFC 9068) and `JWT` accepted; anything else rejected. |
| Claims | `iss` exact match; `exp` / `nbf` with 60s leeway (configurable); `aud` matches when the configured audience appears in a string or array claim. |
| Scopes | Accepted from a space-delimited `scope` string or an `scp` array. Both forms are in the wild. |
| JWKS | Located from `jwks_uri` when configured, otherwise discovered from `issuer` via `/.well-known/oauth-authorization-server`, falling back to `/.well-known/openid-configuration`. Cached; an unknown `kid` triggers a refresh, rate-limited to once per 60s so an attacker cannot drive unbounded fetches with forged `kid` values. |
| `authorization_details` | Extracted into the principal. A malformed claim **denies the request** rather than being treated as absent. |

Opaque tokens with introspection (RFC 7662), DPoP, and mTLS-bound tokens are **out
of scope** for this slice.

### Streamable HTTP transport

- MCP endpoint at `server.http.path`, default `/mcp`. `POST` carries JSON-RPC.
- **`Origin` is validated** against `server.http.allowed_origins` on every request;
  a mismatch is `403`. This is required by the MCP specification and is the
  defense against DNS-rebinding attacks on a locally bound server.
- Default bind is `127.0.0.1`. Binding elsewhere interacts with the guard above.
- **Stateless in v1.** No session storage; `Mcp-Session-Id` is not issued, and each
  request is authenticated independently. SSE streaming (`GET` on the endpoint) is
  deferred — no v1 tool is long-running enough to need it.

### Outbound (per upstream)

| Mode | Behavior |
|---|---|
| `none` | No credential attached. |
| `static` | A fixed secret placed in a configured header. |
| `client_credentials` | RFC 6749 client credentials grant. |
| `token_exchange` | RFC 8693, with the inbound token as `subject_token`. |

**`static`** needs to say where the value goes, since `Authorization: Bearer` and
`X-API-Key` are both common:

```yaml
outbound:
  mode: static
  header: Authorization      # default
  scheme: Bearer             # omit for a bare value, e.g. X-API-Key
  value: ${env:BILLING_API_KEY}
```

**`token_exchange`** configuration:

```yaml
outbound:
  mode: token_exchange
  token_endpoint: https://idp.example.com/oauth2/token
  client_id: sidekit-billing
  client_secret: ${env:BILLING_CLIENT_SECRET}
  audience: https://api.example.com      # or `resource`
  requested_token_type: urn:ietf:params:oauth:token-type:access_token
  scopes: [invoices.write]
```

`token_exchange` requires an inbound token to exchange. Configuring it alongside
`transport: stdio`, or alongside `auth.inbound.enabled: false`, is a **startup
error** — there is no `subject_token` to present, and discovering that at
invocation time would violate §10's rule that config failures surface at startup.

### What `carry: true` carries

**The rule's required detail `R`** — not the presented detail that satisfied it,
and not the caller's full `authorization_details`.

This is least privilege: the upstream token carries exactly the authority the tool
declared it needs, even when the caller holds broader grants. Carrying the
presented detail would propagate authority the tool never asked for; carrying the
whole set is full delegation and defeats the purpose of per-tool RAR.

The carried value is the **accumulated** set of required details for the
operation, which is also what the cache key hashes.

`carry` applies to `client_credentials` as well as `token_exchange` — RFC 9396
permits `authorization_details` on any grant type.

### Token cache

| Mode | Cache key | TTL |
|---|---|---|
| `none`, `static` | Not cached. | — |
| `client_credentials` | `upstream + scopes + hash(carried details)` | Token `exp`, minus leeway. |
| `token_exchange` | `upstream + hash(subject token jti, else the token) + scopes + hash(carried details)` | `min(exchanged exp, subject exp)`, minus leeway. |

The subject token — not the `sub` claim — is part of the exchange key. Keying on
`sub` alone would let two different tokens for the same user share one exchanged
token, so a revoked or expired session would keep working through a cache entry
minted for a different one. Capping TTL at the subject token's expiry stops the
exchanged token outliving the grant it derives from.

`client_credentials` deliberately does **not** include the principal subject: that
grant does not depend on the caller, and including it would multiply IdP calls by
user count for identical tokens. The carried-details hash is included because
details do vary per operation.

Each cache key is single-flighted so a burst of tool calls does not stampede the
IdP.

### RAR coverage semantics

A required detail **R** is covered by the presented set **P** when there exists a
**single** `p ∈ P` satisfying all of:

| Field | Rule |
|---|---|
| `type` | `p.type == R.type` |
| `actions` | `R.actions ⊆ p.actions` |
| `locations` | `R.locations ⊆ p.locations`, exact string equality |
| `datatypes` | `R.datatypes ⊆ p.datatypes` |
| `privileges` | `R.privileges ⊆ p.privileges` |
| `identifier` | `p.identifier == R.identifier` |

Rules apply only to fields `R` actually specifies. **If `R` specifies a field that
`p` lacks, coverage fails.** A request is authorized when every accumulated
requirement is covered.

Coverage must be satisfied by a single presented detail; requirements are not
composed across multiple entries, because composition would let two narrow grants
combine into an authority neither one conveyed.

Location matching is exact string equality in v1. Prefix or hierarchical matching
is deliberately deferred: it introduces boundary questions (does
`https://api/v1/pay` cover `https://api/v1/payments`?) whose wrong answer is a
privilege escalation.

---

## 9. Invocation

1. Resolve the tool name to an `Operation` in the `ToolSet`.
2. Validate arguments against the operation's `input_schema`.
3. Establish the principal (§8).
4. Accumulate policy requirements for the operation and check coverage.
5. Acquire the outbound credential, carrying required details where the rule says so.
6. Build the HTTP request (below).
7. Execute with the upstream's per-attempt timeout.
8. Map the response to an MCP tool result.

### Request construction

`build_request()` is a pure function from `(binding, validated_args, credential)`
to a request object. It is the highest-risk code in the system and is tested as
such.

**Path parameters are strictly percent-encoded.** Each value is encoded against
the RFC 3986 unreserved set, so `/`, `?`, `#`, `%`, and `..` cannot escape their
segment. Without this, `id = "../admin/reset"` on `GET /v1/invoices/{id}` reaches
an operation the registry never exposed — defeating the entire exposure model. An
empty value for a required path parameter is an invocation error, since it would
collapse the segment and change the route.

**Query parameters** serialize per the binding's `style` and `explode`, defaulting
to `form` / `explode: true`, matching OpenAPI defaults. Values are
percent-encoded.

**Header parameters are denylisted at load time.** Any binding — introspected or
explicit — declaring a header parameter whose name case-insensitively matches the
denylist is a **load error**:

`Authorization`, `Proxy-Authorization`, `Host`, `Cookie`, `Set-Cookie`,
`Content-Length`, `Transfer-Encoding`, `Connection`, `Upgrade`, `TE`, `Trailer`,
`Expect`, any `Proxy-*`, any `X-Forwarded-*`, and the header configured by the
upstream's `static` outbound mode.

Rejecting at load rather than at request time means the failure is visible to the
operator who wrote the config rather than to a model at call time. The specific
hazard: `Authorization` as a model-supplied argument bypasses the outbound broker
entirely, turning the sidecar into an open proxy for whatever credential the model
invents.

**Bodies** serialize as JSON from the flattened arguments, reassembled using each
argument's `wire_name`. Credentials are attached last and overwrite any
same-named header.

### Response mapping

| Concern | Behavior |
|---|---|
| JSON | Serialized to text content. |
| `text/*` | Passed through as text. |
| Other content types | Not inlined. The result reports content type and length. |
| Size cap | 1 MiB default, `upstreams.<k>.max_response_bytes` to change. Truncation carries an explicit marker stating that it occurred and the original size. |
| `isError` | Set for any non-2xx response and for transport failures. |
| `structuredContent` / `outputSchema` | **Deferred.** v1 emits text content only. |

---

## 10. Error handling

Error categories are chosen for what a model should *do* about them, since this is
an agent-facing surface.

| Category | Retryable | Result |
|---|---|---|
| Invalid arguments | Yes, after correction | Schema validation message naming the offending field. |
| Authorization denied | No | States which authorization detail was missing, never the token contents. |
| Upstream 4xx | No | Surfaces the upstream's message as a bounded excerpt. |
| Upstream 5xx / timeout | Yes | Subject to the rules below. |
| Configuration error | N/A | Raised at startup, never at call time. |

### Retry

- **Gated on `effect`.** Only `read_only` and `idempotent_write` operations are
  ever retried. `action` operations are never retried, because retrying a POST
  after a timeout is how a customer gets charged twice.
- Retryable conditions: connect and read timeouts, `502`, `503`, `504`.
- `429` is retried honoring `Retry-After`, capped at 10s per wait.
- Maximum 2 retries (3 attempts). Exponential backoff, 100ms base, factor 2, full
  jitter.
- `timeout_ms` is **per attempt**. Total wall clock is additionally capped by
  `max_total_ms`, defaulting to `3 × timeout_ms`.

### Startup validation

These configurations are load errors. Together they are the reason "a config that
would fail on some future tool call is a config that fails to load" is enforceable
rather than aspirational.

1. `mode` absent, or `version` unrecognized.
2. `mode: introspect-unsafe` without `acknowledge_unsafe: true`.
3. `transport: http` + `inbound.enabled: false` + non-loopback host, without
   `allow_unauthenticated_http: true`.
4. `token_exchange` with `transport: stdio` or with inbound disabled.
5. A binding declaring a denylisted header parameter.
6. A literal value in a secret-typed field.
7. An unknown field, or an empty array, in `require.authorization_details[]`.
8. A `require`-less rule under `unmatched: deny`.
9. RFC 9728 `resource` not equal to `auth.inbound.audience`.
10. An unresolvable `policy.file` or `${file:}` path.
11. A tool-name collision surviving hash suffixing, or a re-colliding flattened
    argument name.

Warnings, not errors: a selection glob, tag filter, or policy rule matching zero
operations.

### Logging and redaction

Tokens, secret-reference values, and `Authorization` headers are redacted at the
logging boundary rather than at each call site, so a new log statement cannot leak
by omission. Upstream error bodies are logged and returned as bounded excerpts; an
error path must not be able to dump a megabyte into a context window or a log
aggregator.

---

## 11. Testing

- **Pure unit tests, no I/O:** classification, selection, naming and collision
  resolution, flattening, `build_request()`, policy accumulation, and RAR coverage.
- **Security regression tests**, each mapped to a named hazard:
  - Path traversal: `../`, encoded `%2e%2e%2f`, and absolute-URL values in path
    parameters stay inside their segment.
  - Header injection: every denylisted header is rejected at load, from both the
    explicit and the OpenAPI source; CRLF in a header value is rejected.
  - RAR strictness: unknown field rejected; empty array rejected; `R` field absent
    from `p` fails coverage; malformed claim denies.
  - Carry-only rule under `unmatched: deny` does not make an operation callable.
  - Exchange cache: two distinct subject tokens with the same `sub` do not share a
    cache entry; exchanged TTL never exceeds subject `exp`.
  - JWT: `alg: none` and HS\*-signed-with-public-key rejected.
  - Origin mismatch rejected with 403.
- **Golden tests:** OpenAPI 3.0 and 3.1 fixtures, including dialect edge cases,
  circular refs, and path-level parameters, snapshot to expected `ToolSet`s.
- **Contract tests:** valid and invalid config fixtures; every startup-validation
  case above; schema drift check against model-generated schema.
- **Transport tests:** a real in-process stub HTTP server, not a mocked client.
- **Auth tests:** a local fake IdP issuing JWTs against a test JWKS.
- **Survey tests:** scripted input, asserting emitted config and reconcile against
  an existing config, including YAML comment preservation.

---

## 12. Phasing

Derived from the module dependency graph rather than by feature grouping. Each
phase depends only on phases before it, and each ends with something runnable.

| Phase | Modules | Runnable result |
|---|---|---|
| **P1** | `operations`, `config/models`, `config/loader`, `config/schema`, `sources/explicit`, `classify`, `registry` (merge + selection), `naming`, `transports/http` incl. `build_request()`, `auth/outbound` (`none`, `static`), `server/mcp`, `server/stdio`, `cli` | A stdio sidecar serving an explicitly configured API with a static credential. `mode: configured` only. |
| **P2** | `sources/openapi`, dialect conversion, `x-mcp-*`, `registry` mode posture | Point it at an OpenAPI URL. Adds `introspect-safe` and `introspect-unsafe`. |
| **P3** | `config/policy`, `auth/rar`, `policy`, local principal | RAR enforcement, working under stdio with a locally asserted principal. |
| **P4** | `auth/outbound` (`client_credentials`, `token_exchange`, cache), `auth/inbound`, `server/http` | The full broker over streamable HTTP. |
| **P5** | `configure/survey`, `configure/reconcile` | The interactive authoring workflow. |

Corrections from the previous draft, all dependency violations: `naming` and
`registry` moved from P2 into P1 (the server cannot resolve a tool without them),
`static` outbound moved from P4 into P1 (P1 claimed static credentials while the
module lived three phases later), and `selection` and `classification` moved into
P1 because P1 publishes the schema that declares them. `configured` is now
correctly P1's only mode.

The rule the table follows: **a phase that publishes a config field must implement
it.** Shipping a schema whose fields silently do nothing is the same drift the
generated-schema check exists to prevent, just moved from CI to the contract.

**P4 remains the largest phase.** If it needs splitting during planning, the seam
is inbound (resource server, JWKS, discovery, Origin) versus outbound
(`client_credentials`, exchange, cache) — they share no code, only config.

---

## 13. Deferred

Recorded so later slices inherit intent rather than re-deriving it.

- **gRPC.** New `OperationSource` (reflection or descriptor set) and new
  `GrpcBinding` plus adapter. Inherits classification, selection, naming, policy,
  and the auth broker unchanged. Requires a classification rule for gRPC methods,
  which lack HTTP's method semantics.
- **GraphQL.** Queries map to `read_only`, mutations to `action`. Requires a
  decision on field-selection granularity that HTTP does not raise.
- **DASH, HLS, QUIC.** Long-term interest, not designed for.
- **Hierarchical RAR location matching**, for the escalation risk in §8.
- **Opaque token introspection (RFC 7662), DPoP, mTLS-bound tokens.**
- **SSE streaming and MCP sessions** on the HTTP transport.
- **`structuredContent` / `outputSchema`** on tool results.
- **Form and multipart request bodies.**
- **Runtime introspection refresh.**
- **Metrics, tracing, and request-id propagation to the upstream.** v1
  observability is structured logging only. This is a scope decision, not an
  oversight.
- **MCP resources and prompts.** Tools only.

---

## 14. Decisions and rationale

| Decision | Rationale |
|---|---|
| Normalized operation model at the center | The config format is a published contract; its shape *is* the model's shape. The one part worth designing for generality up front is the part that cannot be refactored later. |
| Binding is a closed, protocol-specific union | Unifying HTTP and gRPC call shapes is where this design would over-fit. |
| JSON Schema generated from Pydantic | A hand-maintained schema drifts from the code that reads it. CI enforces agreement. |
| RAR policy in a separate file | Config content is regenerated from upstream documents; authorization policy must not be. |
| Policy rules accumulate | First-match-wins allows a broad early rule to shadow a stricter later one, and it fails open. |
| Only `require`-bearing rules count as "matched" | Otherwise a carry-only rule flips an operation from denied to callable under `unmatched: deny` — a loosening, contradicting the accumulation guarantee. |
| `require` parsed with `extra="forbid"` | Pydantic's default would silently ignore `privileges` or `identifier`, making a rule broader than written in the one file that must fail closed. |
| Empty arrays rejected in `require` | `∅ ⊆ anything` requires nothing while appearing to require something. |
| Single-detail RAR coverage | Composing coverage across entries would let two narrow grants combine into authority neither conveyed. |
| Exact location matching in v1 | Hierarchical matching has boundary cases whose wrong answer is privilege escalation. |
| `carry` sends the required detail `R` | Least privilege. Carrying the presented detail or the full set propagates authority the tool never asked for. |
| Exchange cache keyed on the subject token | Keying on `sub` lets a revoked session keep working through an entry minted for a different token. |
| Exchanged TTL capped at subject `exp` | A derived token must not outlive the grant it derives from. |
| `id` is the only match key | Names change with strategy, prefixes, truncation, and collisions. A stale name glob fails open in both selection and policy. |
| Naming runs after selection | Collision suffixes depend on the surviving set; naming first lets removing one operation rename another. |
| Path values strictly percent-encoded | `../` in a path parameter otherwise reaches operations the registry never exposed. |
| Header parameters denylisted at load | A model-supplied `Authorization` header bypasses the outbound broker entirely. |
| Unauthenticated HTTP guarded | It is a larger blast radius than `introspect-unsafe`, so it gets at least equal ceremony. |
| `alg` allowlist, `none` and HS\* rejected | Accepting HMAC alongside a public JWKS enables key-confusion. |
| JWKS refresh rate-limited | An unknown-`kid` refresh path is otherwise an unbounded fetch trigger. |
| `Origin` validated | Required by the MCP spec; the defense against DNS rebinding on a locally bound server. |
| External `$ref` disabled by default | The document would otherwise dictate which URLs sidekit fetches — an SSRF vector. |
| OpenAPI `security` ignored | An upstream document must not influence which credential sidekit presents. |
| Effect and sensitivity as separate axes | A read-only endpoint can still be unsafe to expose, and `introspect-safe` needs a precise definition. |
| Sensitivity not settable from policy | Policy runs at invocation; sensitivity is consumed at registry-build time, and `match.sensitivity` would be circular. |
| Effect derived from HTTP method | RFC 9110 already defines safe and idempotent semantics. |
| Retry gated on effect | Retrying a POST after a timeout double-charges customers. |
| HEAD and OPTIONS never exposed | No usable response body; they exist for cache and CORS negotiation. |
| 64-character tool names, fixed | Several MCP clients cap at 64. A configurable limit is a knob nobody tunes. |
| Secrets are references only, enforced by schema | Makes configs safe to commit by construction rather than by discipline. |
| Local principal on stdio and unauthenticated HTTP | Refusing to run policy protects nobody and pushes teams toward a second, weaker local policy file. |
| `unmatched: allow` by default | Policy is opt-in hardening; `deny` provides whitelist mode. |
| Dead rules warn, not fail | A rule matching nothing on one upstream is legitimate in a multi-upstream config. |
| Survey proposes rather than interrogates | A survey that trains its operator to press Enter manufactures the appearance of review. |
| Mode controls exposure, never authorization | Conflating the two turns a debugging convenience into a production incident. |
