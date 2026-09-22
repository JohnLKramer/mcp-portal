# mcp-portal: Docker/Mocks Testing Standard — Design

## 1. Summary

Every phase of mcp-portal should be able to prove its main flow works against
a live backend over the network, not just against pure unit tests and
`httpx.MockTransport`. Today that's only true by accident: `mocks/billing` and
`mocks/orders` (Flask, `docker-compose.yml`, `tests/integration/`) exist, but
the one Docker integration test (`tests/integration/test_stack.py`) exercises
`mode: configured` with **no** outbound credential, no OpenAPI introspection,
and no RAR policy — P1's `static` mode, P2's introspection mode, and P3's
policy enforcement all currently have zero Docker-level coverage.

This spec fixes that going forward (a standing standard every future phase
follows) and retroactively (a new phase, **P4.1**, that closes the gap for
P1–P4a before P4b/inbound work starts). It also standardizes the mock
Dockerfiles' shared layers via Buildx Bake, and starts a lightweight
future-work tracking document for ideas that don't have a design yet.

### Goals

- A Docker-based integration test proves each phase's headline capability,
  not comprehensive coverage — unit tests already own that.
- Mock backends enforce real auth (API key / OAuth) so a passing integration
  test proves the *whole* round trip, including the backend's own
  enforcement, not just that mcp-portal attached something.
- No duplicated Dockerfile boilerplate across the main app and every mock.
- A single, low-ceremony place to track ideas that aren't designed yet.

### Non-goals

- Replacing or duplicating unit test coverage.
- Full OAuth-server functionality — the mock IdP is off-the-shelf, not
  something we build or extend.
- Designing gRPC/GraphQL/DASH/HLS/QUIC/web-crawling — those stay deferred;
  this spec only decides where they're tracked.

---

## 2. Docker/mocks testing standard

- **`mocks/<source>/`** is the standing convention: one Flask app per
  distinct real-world API being simulated, its own `pyproject.toml` /
  `uv.lock` / Dockerfile, always exposing `/healthz`.
- **Prove the main flow, not everything.** Each mock implements just enough
  behavior for its `docker-compose` service to exercise the phase(s) that
  need it — a protected route, a served OpenAPI document, a specific error
  case a policy rule targets. Comprehensive per-endpoint coverage stays a
  unit-test concern (`mocks/<source>/tests/`, already the pattern).
- **Auth is real, not simulated-away.** A mock whose upstream is meant to
  require a credential actually rejects requests without one. It is
  acceptable for a mock's own auth-serving concern to live in the same
  process as its functional routes (`mocks/orders` checking its own API key
  header) — a separate identity-provider process is only worth it when the
  auth protocol itself needs a real implementation (OAuth).
- **`tests/integration/`** keeps its existing shape: `pytest.mark.integration`
  (excluded from the default `uv run pytest` run, invoked via
  `-m integration`, skipped — not failed — when Docker is unavailable). New
  scenarios get their own fixture file alongside the existing
  `fixtures/stack.yaml` (e.g. `stack-introspection.yaml`,
  `stack-policy.yaml`, `stack-oauth.yaml`) rather than overloading one config
  to prove everything at once.
- **Every phase from here forward ships its Docker proof in the same PR as
  the feature**, not as a follow-up. P4.1 exists once, to pay down the debt
  that already accumulated; it is not a recurring pattern later phases lean
  on.

### `mocks/orders`: API key (static outbound mode)

Hardcoded — no rotation, no config surface. Every `/v1/*` route requires
`X-Api-Key: <fixed test value>`; a missing or wrong value is `401`. This is
what proves P1's `outbound.mode: static` (built in P1, never exercised over
Docker) — `static` already models an API key exactly (a fixed value in a
configured header), so no mcp-portal change is needed, only the mock.

### `mocks/billing`: OAuth (`client_credentials` / `token_exchange`)

Uses **`navikt/mock-oauth2-server`** (prebuilt Docker image,
`ghcr.io/navikt/mock-oauth2-server`) as its own `docker-compose` service —
not built from `Dockerfile.base`, since it's a prebuilt third-party image.
Chosen over hand-rolling JWT/JWKS/discovery endpoints because:

- It's purpose-built for exactly this (issuing real signed JWTs, serving
  JWKS + `/.well-known/openid-configuration`/`oauth-authorization-server`).
- It natively supports the RFC 8693 token-exchange grant — the one P4a
  outbound mode that would otherwise need us to build exchange semantics
  into a hand-rolled mock IdP.
- It supports claim templating (token callbacks keyed on grant type / client
  / request params), which is how we inject `authorization_details` into
  issued tokens for RAR-policy Docker tests.

Configured with (at minimum) one `client_credentials` client and one
token-exchange-capable client; `mocks/billing`'s Flask app gains a small
bearer-token check (fetch the mock IdP's JWKS, verify the `Authorization`
header's JWT, `401` on failure/absence) so a passing test proves real
enforcement, not just that mcp-portal attached *something*.

---

## 3. `Dockerfile.base` + Buildx Bake

Docker has no native "inherit another Dockerfile" mechanism; the shared
layers have to become a real image other Dockerfiles build `FROM`. Buildx
Bake's **additional build contexts** feature does this cleanly:

- Root **`Dockerfile.base`**: only what's identical everywhere —
  `python:3.14.7-slim`, `uv` installed, the non-root `appuser` created,
  shared `ENV` (`PATH`, `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`). Two
  stages, `base-builder` (has `uv`, root) and `base-runtime` (has `appuser`,
  no app code yet).
- Root **`docker-bake.hcl`**: a `base` target building that file; every
  service target (`mcp-portal`, `billing-mock`, `orders-mock`) declares
  `contexts = { pybase = "target:base" }`, letting its *own* Dockerfile do
  `FROM pybase AS builder` / `FROM pybase AS runner` and add only what
  genuinely differs per service: manifest copy + `uv sync`, and the final
  `CMD`/`ENTRYPOINT`/`HEALTHCHECK` (the main app uses `mcp-portal serve` as
  entrypoint, no healthcheck, and needs `README.md` present for hatchling's
  `readme=` field; the mocks use `gunicorn` + a healthcheck and skip the
  README).
- Every builder stage uses `RUN --mount=type=cache,target=/root/.cache/uv`
  for `uv sync` — `uv` already exposes exactly the cache directory a
  BuildKit cache mount wants; no host temp directory to provision. Buildx's
  own cross-invocation layer cache (`cache-to`/`cache-from`) is left
  unconfigured for now — only relevant once CI exists, where each run may
  start a fresh builder state; the local `docker buildx` builder already
  persists between invocations.
- One command builds everything in the right order: `docker buildx bake`.

---

## 4. P4.1 — retrofit and enforce

Sits between P4a (outbound; done, merged) and P4b (inbound; not yet started)
in the design spec's phasing table (§12) — it needs P4a's `client_credentials`
to have something to prove, and it needs to land before P4b so that plan
follows the standard from its first task rather than retrofitting again
later.

**Phasing table amendment:** insert a row between P4 and P5:

| Phase | Modules | Runnable result |
|---|---|---|
| **P4.1** | `Dockerfile.base`, `docker-bake.hcl`, `mocks/*` auth, `tests/integration/fixtures/*` | Every phase P1–P4 has a Docker-driven integration test proving its headline capability, including real backend-side auth enforcement. |

### Tasks (high level — the implementation plan TDDs each)

1. `Dockerfile.base` + `docker-bake.hcl`; convert the three existing
   Dockerfiles (`mcp-portal`, `mocks/billing`, `mocks/orders`) to
   `FROM pybase`.
2. `mocks/orders` gains the hardcoded API-key check; extend
   `tests/integration/fixtures/stack.yaml`'s `orders` upstream with
   `auth.outbound.mode: static` — proves P1's `static` mode over Docker.
3. `mocks/billing` gains an `/openapi.json` route describing its existing
   hand-written endpoints; new `stack-introspection.yaml` using
   `mode: introspect-safe` against it — proves P2's live-introspection flow
   over Docker.
4. New `stack-policy.yaml` (a policy file, `defaults.unmatched: deny`, one
   allow rule, one deny rule) run against the existing `configured`-mode
   operations — proves P3's RAR enforcement over Docker.
5. `navikt/mock-oauth2-server` service + `mocks/billing` bearer-token check;
   new `stack-oauth.yaml` using `outbound.mode: client_credentials` — proves
   P4a's dynamic outbound mode over Docker (the one genuinely new capability
   under test here, not a retrofit).
6. `AGENTS.md` updated per §6 below — the standing standard for every future
   phase.

### Notes carried into the P4-inbound plan (not designed here)

- **Blocking, before P4-inbound's Task 1:** the config shape for inbound
  API-key auth needs an explicit design pass before implementation —
  whether it's mutually exclusive with OAuth mode per transport or both can
  be enabled at once, and where a per-key identity/`authorization_details`
  mapping lives. This spec deliberately does not answer it.
- **Suggestion, not a requirement:** P4-inbound's own end-to-end test (its
  Task 9) currently sketches a hand-built fake IdP via
  `httpx.MockTransport`. Worth revisiting to use the same
  `navikt/mock-oauth2-server` Docker pattern this spec establishes, for
  consistency with P4.1's stack tests — evaluate when that plan executes.

---

## 5. Future-work tracking

New, lightweight **`docs/superpowers/specs/future-work.md`** — separate from
the formal design spec so it doesn't carry that document's full self-review
ceremony. One short entry per idea, no design, explicitly including items
that don't have a library decision yet (see §6) rather than omitting them
until they do:

- **Web resource crawling for allow/deny-listing** (new): crawl target sites
  to help generate a whitelist or blacklist of exposable endpoints/resources
  for `x-mcp-exclude`-style curation. Priority not yet set. Includes a
  sub-note on quick-and-dirty text summarization of crawled pages, as a
  likely helper for that same feature rather than an independent capability
  — flag if that grouping is wrong.
- A pointer back to the main design spec's existing §13 (Deferred) for
  already-tracked items (gRPC, DASH/HLS/QUIC, hierarchical RAR location
  matching, opaque-token introspection/DPoP/mTLS, SSE streaming/MCP
  sessions, `structuredContent`/`outputSchema`, form/multipart bodies,
  runtime introspection refresh, metrics/tracing, MCP resources/prompts) —
  one place to scan at a glance, without forking the source of truth.

---

## 6. AGENTS.md: Technology Choices reference

A new table, so technology decisions stop being tribal knowledge:

| Concern | Choice |
|---|---|
| OpenAPI parsing | hand-rolled (`sources/openapi*.py`) + `jsonschema` |
| RAR (RFC 9396) | hand-rolled (`auth/rar.py`, `policy.py`) — small predicate, no library |
| Streaming HTTP transport | `mcp` SDK (`StreamableHTTPSessionManager`, P4b) |
| stdio transport | `mcp` SDK (`stdio_server`, P1) |
| JWT | `pyjwt[crypto]` (P4b) |
| Mock HTTP backends | Flask + `gunicorn` |
| Mock OAuth IdP | `navikt/mock-oauth2-server` |
| gRPC | not yet decided — undesigned, see `future-work.md` / spec §13 |
| GraphQL | not yet decided — designed at a high level (spec §15), P6, no library chosen |
| DASH / HLS / QUIC | not yet decided — undesigned, see spec §13 |
| Web crawling / resource allow-deny-listing | not yet decided — undesigned, see `future-work.md` |

Undesigned items are listed deliberately, not omitted — the point is a
single place to see "not decided yet" alongside "decided," not to hide the
gaps until someone remembers to ask.

---

## 7. Testing

- P4.1's own tests are the Docker integration tests it adds (§4) — no new
  unit-testing pattern introduced.
- `Dockerfile.base`/Bake changes are validated by `docker buildx bake`
  succeeding and the existing `tests/integration` suite passing against the
  resulting images.
- Mock auth changes (`mocks/orders` API key, `mocks/billing` bearer check)
  get their own small unit tests in `mocks/<source>/tests/`, matching the
  existing per-mock unit-test convention — a missing/wrong credential
  returns the expected `401`, a valid one passes through.

---

## 8. Decisions and rationale

| Decision | Rationale |
|---|---|
| Docker proof is "headline flow," not full coverage | Unit tests already own comprehensive coverage; Docker tests exist to catch integration-boundary problems unit tests can't see. |
| Mock auth is real, not simulated away | A passing test must prove the backend actually enforces the credential, not merely that mcp-portal sent one. |
| Auth allowed on the same server as its functional source | Only worth a separate process when the protocol itself (OAuth) needs a real implementation; a hardcoded API key doesn't. |
| `navikt/mock-oauth2-server` over hand-rolled JWT/JWKS | Native RFC 8693 token-exchange support and claim templating for `authorization_details`, both of which we'd otherwise have to build ourselves. |
| Buildx Bake over a Makefile-driven base image | Native shared-base-target support (`contexts = target:base`) rather than a hand-rolled build-order script; accepted as new tooling surface in exchange for correctness. |
| `uv`'s cache dir doubles as the BuildKit cache-mount target | No separate host temp directory to provision or document. |
| P4.1 is a one-time retrofit, not a recurring phase | Every phase from here forward ships its own Docker proof in the same PR as the feature; P4.1 only exists to pay down the debt that predates this standard. |
| Undesigned tech choices are listed as "not yet decided," not omitted | A single place to see what's decided and what's still open, rather than gaps that only surface when someone asks. |
