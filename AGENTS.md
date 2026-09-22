# Project Overview

mcp-portal is an MCP (Model Context Protocol) gateway that exposes HTTP/OpenAPI
backend endpoints as MCP tools, with explicit opt-in exposure and RAR
(RFC 9396) policy enforcement on every call. It serves over stdio or
streamable HTTP to AI clients (Claude Desktop, Claude Code, etc.). Currently
P4: OpenAPI introspection, `x-mcp-*` opt-in annotations, RAR policy
enforcement, the HTTP transport, inbound OAuth (JWT validation, JWKS,
RFC 9728 discovery), and outbound `client_credentials`/`token_exchange` are
implemented; gRPC/GraphQL backends, JSONPath response filtering, and rate
limiting are not.

**Tech stack:** Python, uv, Pydantic, httpx, ruamel.yaml, jsonschema, the `mcp`
SDK, pytest, ruff, mypy, Hatchling.

## Repository Structure

- `src/mcp_portal/` — application source.
  - `app.py` — builds the app (registry, policy engine, principal) from config.
  - `cli.py` — `validate` / `serve` CLI entrypoints.
  - `classify.py`, `naming.py`, `operations.py`, `policy.py`, `registry.py` —
    operation classification, name generation, the `Operation` model, RAR
    policy engine, and toolset assembly.
  - `auth/` — `principal.py` (locally-asserted principal, and the
    HTTP-bearer-token-derived principal for `transport: http`), `rar.py` (RAR
    coverage predicate), `inbound.py` (RFC 9068 JWT verification, JWKS/AS
    discovery), `token_cache.py` (shared in-memory token cache, used by both
    inbound JWKS caching and dynamic outbound credentials), `outbound.py`
    (outbound credential handling — `static`, `client_credentials`,
    `token_exchange` sources).
  - `config/` — `models.py` (Pydantic config models), `loader.py` (load +
    validate config), `policy.py` (policy-file models), `schema.py`
    (generates `schema/config-v1.schema.json`).
  - `sources/` — operation sources: `explicit.py` (config-defined),
    `openapi.py` + `openapi_document.py` + `openapi_paths.py` + `dialect.py` +
    `refs.py` (OpenAPI introspection), `merge.py` (merges sources by `id`),
    `flatten.py`.
  - `server/` — `mcp.py` (`ToolInvoker`, MCP server wiring), `stdio.py`
    (stdio transport entrypoint), `http.py` (streamable HTTP transport:
    `mcp` SDK ASGI app assembly, inbound bearer-token verification wiring,
    per-request principal resolution).
  - `transports/` — `base.py`, `http.py` (upstream HTTP execution, response
    size capping/truncation).
- `tests/` — unit tests (one file per source module) plus `integration/`
  (Docker-gated) and `fixtures/`.
- `examples/` — sample configs (`billing.yaml`, `billing/`, `orders/`) used by
  tests and the quickstart.
- `schema/` — generated `config-v1.schema.json`; regenerate, don't hand-edit.
- `mocks/` — mock backend(s) used by integration tests.

Planning/spec artifacts (design specs, phase-by-phase implementation plans)
are gitignored (`docs/superpowers/`, `nimbalyst-local/`, `scratch/`) — see
Agent Guardrails below.

## Build & Development Commands

```bash
uv sync                              # install deps
uv run mcp-portal validate --config <path>   # validate config, list tools
uv run mcp-portal serve    --config <path>   # serve over stdio
uv run pytest                        # unit tests (excludes integration)
uv run pytest -m integration         # integration tests (requires Docker)
docker buildx bake                   # build all Docker images (main app + mocks)
uv run ruff format .                 # format
uv run ruff check .                  # lint
uv run mypy src                      # type-check
uv run python -m mcp_portal.config.schema   # regenerate schema/config-v1.schema.json
```

> TODO: no CI workflow file exists yet (`.github/` only has `CODEOWNERS`); how
> these commands are gated in CI is not defined in-repo.

## Architecture Notes

Config load → operation sourcing → merge → classify → policy check → execute:

1. `config/loader.py` loads and validates the main config (and, if present,
   the policy file via `config/policy.py`), resolving `${env:}`/`${file:}`
   secret references and `policy.file` relative to the config's directory.
2. `sources/explicit.py` and/or `sources/openapi.py` produce `Operation`
   records; `mode` (`configured` / `introspect-safe` / `introspect-unsafe`)
   controls which sources run. `sources/merge.py` merges them by `id`.
3. `classify.py` derives `effect` from HTTP method and applies `sensitivity`
   classification rules; `registry.py` selects and builds the toolset;
   `naming.py` generates tool names (never a stable match key — see below).
4. Establish the principal — this varies by transport. Under stdio, it is
   always the self-asserted local principal (`auth/principal.py`'s
   `local_principal`) — a guardrail, not a security boundary. Under
   `transport: http`, `server/http.py` resolves it per request: with
   `auth.inbound.enabled`, `auth/inbound.py`'s `JwtTokenVerifier` validates
   the caller's bearer JWT (RFC 9068, JWKS-backed) and
   `principal_from_access_token` builds a principal from its
   `authorization_details` claim and the token itself (kept as
   `subject_token` for a later exchange); with inbound auth disabled, HTTP
   falls back to the same local principal as stdio.
5. `server/mcp.py`'s `ToolInvoker` validates arguments, then calls `policy.py`
   (the RAR engine) with that principal — denial happens here, before the
   upstream is ever called — then executes via `transports/http.py`, which
   asks the upstream's `auth/outbound.py` credential source (`static`,
   `client_credentials`, or `token_exchange`) for a credential, passing along
   any policy-`carry`d `authorization_details` and, for `token_exchange`, the
   inbound `subject_token` to exchange (RFC 8693).

Key invariants (see the local, gitignored design spec under
`docs/superpowers/specs/` for the full rationale):

- **Match on `id`, never on the generated tool name.** Names change with
  naming strategy, prefixing, truncation, and collision suffixes.
- **Secrets are references, not literals** (`${env:VAR}` / `${file:path}`);
  a literal value is a load error.
- **Header bindings are denylisted**: `Authorization`, `Host`, `Cookie`,
  `X-Forwarded-*`, and the configured credential header can never be bound
  from a tool argument.
- **Config problems fail at `validate`/startup, never mid-call.**
- **Policy rules accumulate** (union of matching rules' `require`); they are
  never first-match-wins.
- **`transport: http` with `auth.inbound.enabled: false` on a non-loopback
  host is a startup error** unless `auth.inbound.allow_unauthenticated_http`
  is explicitly set — an unauthenticated endpoint holding a service
  credential is never a silent default.
- **`outbound.mode: token_exchange` requires `transport: http` with
  `auth.inbound.enabled: true`** — a startup error otherwise, since there is
  no inbound `subject_token` to exchange under any other transport/auth
  combination.

## Testing

```bash
uv run pytest              # unit tests, excludes integration (pytest.ini default)
uv run pytest -m integration   # integration tests, requires Docker
```

One test file per source module under `tests/` (e.g. `test_registry.py` for
`registry.py`). `tests/test_p3_end_to_end.py` exercises the full RAR
enforcement path against the design's billing example over stdio;
`tests/test_p4_end_to_end.py` does the same over streamable HTTP, adding
inbound JWT verification and an outbound `token_exchange` call. Golden
fixtures for
OpenAPI parsing live under `tests/fixtures/`; integration test Docker fixtures
live in `tests/integration/fixtures/` (`stack.yaml`, `stack-introspection.yaml`,
`stack-policy-denied.yaml`, `stack-policy-authorized.yaml`, `stack-oauth.yaml`).

> TODO: no CI workflow currently runs these commands (see Build & Development
> Commands above) — verify before assuming PRs are gated on tests/lint/types.

## Technology Choices

| Concern | Choice |
|---|---|
| OpenAPI parsing | hand-rolled (`sources/openapi*.py`) + `jsonschema` |
| RAR (RFC 9396) | hand-rolled (`auth/rar.py`, `policy.py`) — small predicate, no library |
| Streaming HTTP transport | `mcp` SDK (`StreamableHTTPSessionManager`, P4b) |
| stdio transport | `mcp` SDK (`stdio_server`, P1) |
| JWT | `pyjwt[crypto]` (P4b; also mocks/billing in P4.1) |
| Mock HTTP backends | Flask + `gunicorn` |
| Mock OAuth IdP | `navikt/mock-oauth2-server` |
| gRPC | not yet decided — undesigned, see `future-work.md` / design spec §13 |
| GraphQL | not yet decided — designed at a high level (design spec §15), P6, no library chosen |
| DASH / HLS / QUIC | not yet decided — undesigned, see design spec §13 |
| Web crawling / resource allow-deny-listing | not yet decided — undesigned, see `future-work.md` |

Undesigned items are listed deliberately, not omitted — this is a single
place to see what's decided and what's still open.

## Docker/Mocks Testing Standard

- `mocks/<source>/` is one Flask app per distinct real-world API being
  simulated, its own `pyproject.toml`/`uv.lock`/Dockerfile, always exposing
  `/healthz`. Each mock implements just enough behavior — a protected
  route, a served OpenAPI document — to exercise the phase(s) that need it.
  Comprehensive per-endpoint coverage stays a unit-test concern
  (`mocks/<source>/tests/`).
- A mock whose upstream is meant to require a credential actually enforces
  it — a passing integration test must prove the backend rejected an
  unauthenticated call, not merely that mcp-portal attached one. It's fine
  for a mock's own auth-serving concern to live in the same process as its
  functional routes; a separate identity-provider process is only worth it
  when the protocol itself needs a real implementation (OAuth).
- `docker buildx bake` builds every image (`Dockerfile.base` + `docker-bake.hcl`
  supply the shared Python/`uv`/non-root-user layers via Buildx's
  additional-build-context mechanism); `docker compose up` only runs
  already-built images — Compose cannot resolve a Dockerfile's `FROM
  pybuilder`/`FROM pyruntime` bake-context references on its own.
- `tests/integration/` stays `pytest.mark.integration`-gated, skipped (not
  failed) without Docker. New scenarios get their own fixture file
  alongside `fixtures/stack.yaml` rather than overloading one config to
  prove everything.
- Every phase from here forward ships its Docker proof in the same PR as
  the feature.

## Agent Guardrails

- **Always update `README.md` in the same change whenever project behavior,
  status/phase, config surface, or supported features change.** The README
  must stay an accurate description of what's actually implemented — never
  describe unimplemented or roadmap features as current capability. Move
  not-yet-built items to the README's Roadmap section instead.
- **`schema/config-v1.schema.json` is generated, not hand-edited.** Regenerate
  it with `uv run python -m mcp_portal.config.schema` after changing
  `config/models.py`.
- **Respect phase scope.** Each local `docs/superpowers/plans/*.md` file
  states its own phase's global constraints (what is explicitly out of scope
  for that phase) — don't add config fields or behavior for a later phase
  while implementing an earlier one.
- **Never weaken the security invariants** listed under Architecture Notes
  (header denylist, `id`-only matching, secrets-as-references, RAR
  accumulate-never-first-match) without updating the design spec first.
- `.coverage`, `htmlcov/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`,
  `.venv/` are generated — do not hand-edit or commit changes to them.
- **Never commit planning or spec artifacts.** This is a public repo;
  intermediate planning docs (`docs/superpowers/plans/`,
  `docs/superpowers/specs/`), scratch notes (`scratch/`), and
  `nimbalyst-local/` are gitignored and must stay local-only — never `git
  add` or stage them, even incidentally via `git add -A`/`git add .`. If you
  need durable planning docs, keep them in one of those gitignored
  directories, not tracked in the repo.
