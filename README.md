# mcp-portal

[Changelog](CHANGELOG.md) · [License](LICENSE) · [Contributing](CONTRIBUTING.md)

Handing an LLM raw API keys or shell access is a gamble. mcp-portal lets you
give AI clients exactly the backend endpoints you choose as MCP tools. Every
call is checked against an RFC 9396 policy before it reaches your API, and
your secrets never enter the model's reach.

## Table of Contents

- [Quickstart](#quickstart)
- [Why mcp-portal](#why-mcp-portal)
- [Build a config interactively (`configure`)](#build-a-config-interactively-configure)
- [Turn your OpenAPI spec into tools](#turn-your-openapi-spec-into-tools)
- [Control every call with RAR policy](#control-every-call-with-rar-policy)
- [Serve over stdio or HTTP](#serve-over-stdio-or-http)
- [How it works](#how-it-works)
- [Development](#development)

## Quickstart

```bash
uv sync

export BILLING_API_KEY=sk-test   # the example config references this

uv run mcp-portal validate --config examples/billing.yaml   # check config, list tools
uv run mcp-portal serve    --config examples/billing.yaml   # serve over stdio
```

Register it with an AI client as a stdio MCP server, e.g. in
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mcp-portal": {
      "command": "uv",
      "args": ["run", "mcp-portal", "serve", "--config", "/path/to/config.yaml"]
    }
  }
}
```

See [`examples/billing.yaml`](examples/billing.yaml) for a full commented
config.

## Why mcp-portal

- **Centralizes access control.** Every tool call passes through one gateway
  and is authorized by an RAR policy before anything reaches your backend.
- **Generates tools from OpenAPI.** Point mcp-portal at an OpenAPI 3.0/3.1
  document and its operations become MCP tools. You don't write any tool
  code.
- **Exposes nothing by accident.** An operation is exposed, or exposed with
  restrictions, only because an `x-mcp-*` extension or an explicit config
  entry says so.
- **Protects the model's context window.** Oversized upstream responses are
  truncated, with a marker that says truncation happened and gives the
  original size.
- **Fails fast.** `validate` catches bad config, missing secrets, and policy
  problems before the server starts, so they never surface mid-call.

## Build a config interactively (`configure`)

Instead of hand-writing `operations[]` and a policy file, point `configure`
at an OpenAPI document and answer its prompts:

```bash
uv run mcp-portal configure --config my-service.yaml
```

`configure` introspects the upstream and walks you through it group by
group, asking whether to expose each set of operations. It proposes sensible
defaults (the default answer is yes), so you confirm a proposal instead of
answering every operation from scratch. Before it writes anything, it shows
you the diff to both the config and the policy file, and it only writes if
you confirm.

**Run it again after the upstream API changes** and it reconciles instead of
re-asking everything:

- **New operations** get surveyed.
- **Removed operations** are called out by name, never silently dropped.
- **Changed operations** get re-confirmed.
- **Everything else** is left exactly as you last set it.

## Turn your OpenAPI spec into tools

Choose how tools are built with the `mode` setting:

- **`configured`** uses only your explicit config entries.
- **`introspect-safe`** and **`introspect-unsafe`** derive tools from a live
  OpenAPI document (`sources/openapi.py`).

Control whether and how an introspected operation is exposed with `x-mcp-*`
extensions: `x-mcp-exclude`, `x-mcp-sensitive`, `x-mcp-name`, `x-mcp-title`,
and `x-mcp-description`.

```yaml
paths:
  /invoices/{id}:
    get:
      operationId: getInvoice
      x-mcp-sensitive: true
      x-mcp-description: "Fetch a single invoice by ID."
```

An operation with no `x-mcp-*` extensions is still exposed in
`introspect-safe`/`introspect-unsafe` mode unless you exclude it. Use
`x-mcp-exclude: true` to keep it out of the tool registry entirely.

## Control every call with RAR policy

Every call is checked against the calling principal **before the upstream is
ever invoked**. A denied call never touches your backend.

- **Rules match operations** by `id`, `tags`, `upstream`, `effect`, and
  `sensitivity`.
- **Requirements add up.** Each matching rule adds its `require`d
  `authorization_details`, and the caller must satisfy all of them.
- **Each operation has two risk axes:**
  - **`effect`** (`read_only` / `idempotent_write` / `action`) is derived from
    the HTTP method and decides whether a call can be retried automatically.
  - **`sensitivity`** (`normal` / `sensitive`) is set explicitly by
    classification rules and is never derived.

## Serve over stdio or HTTP

Serve over **stdio** for local AI clients, as in the [Quickstart](#quickstart).
Set `transport: http` to serve over the `mcp` SDK's **streamable HTTP**
transport instead. It comes with the Origin/Host DNS-rebinding defense and
RFC 9728 protected-resource metadata built in.

See [`examples/billing-http.yaml`](examples/billing-http.yaml) for the
streamable-HTTP counterpart of the billing example, with inbound OAuth and
outbound `token_exchange`.

## How it works

```text
AI Client  --stdio-->  mcp-portal (Gateway)  --HTTP-->  OpenAPI backend
```

The gateway loads a config and builds its tool registry from explicit config
entries and/or OpenAPI introspection. For each call, it checks RAR policy,
then forwards the call to the upstream over HTTP. The full rationale is in
the [design spec](docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md).

### Inbound OAuth and outbound credentials

Under `transport: http`, `auth.inbound` validates the caller's own bearer
JWT (RFC 9068, JWKS-backed) and evaluates RAR policy against its
`authorization_details` claim.

An upstream configured with `outbound.mode: client_credentials` gets its own
service credential. One configured with `outbound.mode: token_exchange`
exchanges the caller's token (RFC 8693) for its own credential. When a
matching rule sets `outbound.carry: true`, the credential carries only the
policy's required detail, never the whole set the caller presented.

### Secrets stay out of config and out of the model's reach

- **Secrets are references.** Every secret field is a `${env:VAR}` or
  `${file:path}` reference, never a literal.
- **Credential headers are off-limits.** A denylist blocks bindings that
  would let a model supply `Authorization`, `Host`, `Cookie`, or
  `X-Forwarded-*` headers.

### Config schema

Configs, in JSON or YAML, are validated against
[`schema/config-v1.schema.json`](schema/config-v1.schema.json), which is
generated from the Pydantic models via
`uv run python -m mcp_portal.config.schema`.

## Development

```bash
uv run pytest                  # tests
uv run pytest -m integration   # integration tests (requires Docker)
docker buildx bake             # build all Docker images
uv run ruff format .           # format
uv run ruff check .            # lint
uv run mypy src                # types
```
