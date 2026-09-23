# mcp-portal

mcp-portal is an MCP gateway for HTTP/OpenAPI backends. Instead of giving an
LLM raw system or API access, it exposes specific, opted-in backend endpoints
as structured MCP tools.

**Status: P5.** OpenAPI introspection, opt-in exposure via `x-mcp-*`
extensions, RAR (RFC 9396) policy enforcement, outbound `client_credentials`
and `token_exchange` auth, the streamable HTTP transport, inbound OAuth
(JWT validation, JWKS, RFC 9728 discovery), and an interactive `configure`
command for authoring and reconciling a config are implemented — servable
over stdio or HTTP. Non-HTTP backends are not implemented — see
[Roadmap](#roadmap) and the
[design spec](docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md).

## Key benefits

- **Central access control** — every tool call passes through one gateway,
  authorized by an RAR policy evaluated against a locally-asserted principal.
- **Dynamic tool generation from OpenAPI** — point mcp-portal at an OpenAPI
  3.0/3.1 document and its operations become MCP tools, no hand-written tool
  code required.
- **Explicit opt-in exposure** — an operation is only exposed, or exposed with
  restrictions, because an `x-mcp-*` extension or explicit config entry says
  so. Nothing is exposed by accident.
- **Response size capping** — oversized upstream responses are truncated with
  an explicit marker stating that truncation happened and the original size,
  so a large response can't blow out an LLM's context window.
- **Config fails fast** — `validate` catches bad config, missing secrets, and
  policy problems before the server starts; failures never surface mid-call.

## How it works

```
AI Client  --stdio-->  mcp-portal (Gateway)  --HTTP-->  OpenAPI backend
```

The gateway loads a config, builds its tool registry from explicit config
entries and/or OpenAPI introspection, evaluates each call against RAR policy,
then forwards it to the upstream over HTTP.

## Core features

- **Dynamic tool generation** — `configured` mode uses explicit config
  entries; `introspect-safe`/`introspect-unsafe` modes derive tools from a
  live OpenAPI document (`sources/openapi.py`).
- **Opt-in security via `x-mcp-*`** — `x-mcp-exclude`, `x-mcp-sensitive`,
  `x-mcp-name`, `x-mcp-title`, and `x-mcp-description` control whether and how
  an introspected operation is exposed.
- **RAR policy enforcement** — policy rules match operations by `id`, `tags`,
  `upstream`, `effect`, and `sensitivity`, accumulate `require`d
  `authorization_details`, and are checked against the calling principal
  before the upstream is ever invoked.
- **Two risk axes per operation** — `effect` (`read_only` / `idempotent_write`
  / `action`) is derived from the HTTP method and gates automatic retries;
  `sensitivity` (`normal` / `sensitive`) is set explicitly via classification
  rules, never derived.
- **Secrets stay out of config and out of the model's reach** — every secret
  field is a `${env:VAR}` or `${file:path}` reference, never a literal; a
  denylist blocks bindings that would let a model supply `Authorization`,
  `Host`, `Cookie`, or `X-Forwarded-*` headers.
- **HTTP transport for the gateway itself** — `transport: http` serves over
  the `mcp` SDK's streamable HTTP transport, with the Origin/Host
  DNS-rebinding defense and RFC 9728 protected-resource metadata built in.
- **Inbound OAuth and outbound `client_credentials`/`token_exchange`** —
  under `transport: http`, `auth.inbound` validates the caller's own bearer
  JWT (RFC 9068, JWKS-backed) and evaluates RAR policy against its
  `authorization_details` claim; an upstream configured with
  `outbound.mode: token_exchange` then exchanges that same token (RFC 8693)
  for its own credential, carrying only the policy's required detail — never
  the caller's whole presented set — when a matching rule sets
  `outbound.carry: true`.
- **Interactive `configure` command** — introspects an upstream and walks you
  through a survey of which operations to expose, proposing sensible
  defaults so you're confirming rather than answering from scratch. It shows
  you the config and policy diff and only writes after you confirm. Run it
  again after the upstream API changes and it reconciles instead of
  re-asking everything: new operations get surveyed, removed ones are called
  out instead of silently dropped, changed operations get re-confirmed, and
  everything else is left exactly as it was.

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
config, [`examples/billing-http.yaml`](examples/billing-http.yaml) for the
streamable-HTTP counterpart (inbound OAuth + outbound `token_exchange`), and
[`schema/config-v1.schema.json`](schema/config-v1.schema.json)
(generated from the Pydantic models via `uv run python -m mcp_portal.config.schema`)
for the schema JSON and YAML configs are validated against.

## Authoring a config interactively

Instead of hand-writing `operations[]` and a policy file, point `configure`
at an OpenAPI document and answer its prompts:

```bash
uv run mcp-portal configure --config my-service.yaml
```

`configure` introspects the upstream, then asks group by group whether to
expose a set of operations — the default answer is yes, so you're
confirming a proposal instead of typing an answer from scratch for every
operation. Before writing anything, it shows you the diff to the config and
to the policy file, and it only writes if you confirm.

Run it again later, after the upstream API has changed, and it reconciles
instead of re-asking everything: new operations get surveyed, operations the
upstream removed are called out by name (never silently dropped), operations
whose shape changed get re-confirmed, and everything else is left exactly as
you last set it.

## Annotating backends

Mark an OpenAPI operation safe to expose with `x-mcp-*` extensions:

```yaml
paths:
  /invoices/{id}:
    get:
      operationId: getInvoice
      x-mcp-sensitive: true
      x-mcp-description: "Fetch a single invoice by ID."
```

An operation with no `x-mcp-*` extensions is still exposed in
`introspect-safe`/`introspect-unsafe` mode unless excluded — use
`x-mcp-exclude: true` to keep it out of the tool registry entirely.

## Roadmap

Not implemented yet, tracked in the [design spec](docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md):

- gRPC and GraphQL backends (HTTP/OpenAPI only today)
- JSONPath-based response filtering (byte-size truncation only today)
- Per-call rate limiting

## Development

```bash
uv run pytest            # tests
uv run ruff format .     # format
uv run ruff check .      # lint
uv run mypy src          # types
```
