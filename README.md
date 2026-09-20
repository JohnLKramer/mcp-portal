# mcp-portal

An MCP sidecar for HTTP services. Point it at an API, get MCP tools.

**Status: P1.** Explicit config over stdio with a static credential. OpenAPI
introspection, RAR policy, OAuth, and the HTTP transport land in later phases —
see the [design spec](docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md).

## Install

```bash
uv sync
```

## Use

```bash
export BILLING_API_KEY=sk-test   # the example config references this

uv run mcp-portal validate --config examples/billing.yaml   # check config, list tools
uv run mcp-portal serve    --config examples/billing.yaml   # serve over stdio
```

`validate` exits non-zero on any configuration problem, including an unset secret
variable — config failures surface before the server starts, never mid-call.

With Claude Desktop or Claude Code, register it as an stdio MCP server running
`mcp-portal serve --config /path/to/config.yaml`.

## Configuration

JSON and YAML are both accepted and validated against
[`schema/config-v1.schema.json`](schema/config-v1.schema.json), which is generated
from the Pydantic models — regenerate it with
`uv run python -m mcp_portal.config.schema`.

See [`examples/billing.yaml`](examples/billing.yaml) for a commented config.

### Things worth knowing

**Secrets are references, never literals.** Every secret field takes
`${env:VAR}` or `${file:path}`. A literal value fails to load, so configs stay
safe to commit. File paths resolve relative to the config file, not your shell's
working directory.

**Risk is two axes, not one.** `effect` (`read_only` / `idempotent_write` /
`action`) is derived from the HTTP method and drives the MCP annotations clients
use to warn users — and gates retries, since an `action` is never retried
automatically. `sensitivity` (`normal` / `sensitive`) is never derived; set it
via `classification` rules. A `GET` can be read-only and still unsafe to expose.

**`id` is the only match key.** Selection and classification match on `id`,
`tags`, `upstream`, and `effect` — never on the generated tool name, which
changes with prefixes, truncation, and collision suffixes.

**Header parameters are denylisted.** A binding that declares `Authorization`,
`Host`, `Cookie`, `X-Forwarded-*`, or your configured credential header fails to
load, so a model cannot supply a header that bypasses the outbound credential.

**Config problems fail at startup, never mid-call.**

## Development

```bash
uv run pytest            # tests
uv run ruff format .     # format
uv run ruff check .      # lint
uv run mypy src          # types
```
