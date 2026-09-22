# Project Overview

mcp-portal is an MCP (Model Context Protocol) gateway that exposes HTTP/OpenAPI
backend endpoints as MCP tools, with explicit opt-in exposure and RAR
(RFC 9396) policy enforcement on every call. It serves over stdio to AI
clients (Claude Desktop, Claude Code, etc.). Currently P3: OpenAPI
introspection, `x-mcp-*` opt-in annotations, and RAR policy enforcement are
implemented; HTTP transport, inbound/outbound OAuth, gRPC/GraphQL backends,
JSONPath response filtering, and rate limiting are not.

**Tech stack:** Python, uv, Pydantic, httpx, ruamel.yaml, jsonschema, the `mcp`
SDK, pytest, ruff, mypy, Hatchling.

## Repository Structure

- `src/mcp_portal/` — application source.
  - `app.py` — builds the app (registry, policy engine, principal) from config.
  - `cli.py` — `validate` / `serve` CLI entrypoints.
  - `classify.py`, `naming.py`, `operations.py`, `policy.py`, `registry.py` —
    operation classification, name generation, the `Operation` model, RAR
    policy engine, and toolset assembly.
  - `auth/` — `principal.py` (locally-asserted principal), `rar.py` (RAR
    coverage predicate), `outbound.py` (outbound credential handling).
  - `config/` — `models.py` (Pydantic config models), `loader.py` (load +
    validate config), `policy.py` (policy-file models), `schema.py`
    (generates `schema/config-v1.schema.json`).
  - `sources/` — operation sources: `explicit.py` (config-defined),
    `openapi.py` + `openapi_document.py` + `openapi_paths.py` + `dialect.py` +
    `refs.py` (OpenAPI introspection), `merge.py` (merges sources by `id`),
    `flatten.py`.
  - `server/` — `mcp.py` (`ToolInvoker`, MCP server wiring), `stdio.py`
    (stdio transport entrypoint).
  - `transports/` — `base.py`, `http.py` (upstream HTTP execution, response
    size capping/truncation).
- `tests/` — unit tests (one file per source module) plus `integration/`
  (Docker-gated) and `fixtures/`.
- `examples/` — sample configs (`billing.yaml`, `billing/`, `orders/`) used by
  tests and the quickstart.
- `schema/` — generated `config-v1.schema.json`; regenerate, don't hand-edit.
- `docs/superpowers/specs/` — design spec; `docs/superpowers/plans/` —
  phase-by-phase (P1/P2/P3) implementation plans.
- `mocks/` — mock backend(s) used by integration tests.

## Build & Development Commands

```bash
uv sync                              # install deps
uv run mcp-portal validate --config <path>   # validate config, list tools
uv run mcp-portal serve    --config <path>   # serve over stdio
uv run pytest                        # unit tests (excludes integration)
uv run pytest -m integration         # integration tests (requires Docker)
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
4. `server/mcp.py`'s `ToolInvoker` validates arguments, then calls `policy.py`
   (the RAR engine) with the `auth/principal.py`-built local principal —
   denial happens here, before the upstream is ever called — then executes
   via `transports/http.py`.

Key invariants (see `docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md`
for the full rationale):

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

## Testing

```bash
uv run pytest              # unit tests, excludes integration (pytest.ini default)
uv run pytest -m integration   # integration tests, requires Docker
```

One test file per source module under `tests/` (e.g. `test_registry.py` for
`registry.py`). `tests/test_p3_end_to_end.py` exercises the full RAR
enforcement path against the design's billing example. Golden fixtures for
OpenAPI parsing live under `tests/fixtures/`.

> TODO: no CI workflow currently runs these commands (see Build & Development
> Commands above) — verify before assuming PRs are gated on tests/lint/types.

## Agent Guardrails

- **Always update `README.md` in the same change whenever project behavior,
  status/phase, config surface, or supported features change.** The README
  must stay an accurate description of what's actually implemented — never
  describe unimplemented or roadmap features as current capability. Move
  not-yet-built items to the README's Roadmap section instead.
- **`schema/config-v1.schema.json` is generated, not hand-edited.** Regenerate
  it with `uv run python -m mcp_portal.config.schema` after changing
  `config/models.py`.
- **Respect phase scope.** Each `docs/superpowers/plans/*.md` file states its
  own phase's global constraints (what is explicitly out of scope for that
  phase) — don't add config fields or behavior for a later phase while
  implementing an earlier one.
- **Never weaken the security invariants** listed under Architecture Notes
  (header denylist, `id`-only matching, secrets-as-references, RAR
  accumulate-never-first-match) without updating the design spec first.
- `.coverage`, `htmlcov/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`,
  `.venv/` are generated — do not hand-edit or commit changes to them.
