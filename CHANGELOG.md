# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-09-24

First release. mcp-portal is an MCP gateway that exposes selected HTTP/OpenAPI
backend endpoints as MCP tools, with policy checks on every call.

### Added

- **Tool generation** from explicit config entries or a live OpenAPI 3.0/3.1
  document (`configured`, `introspect-safe`, and `introspect-unsafe` modes).
- **Opt-in exposure** controlled by `x-mcp-*` OpenAPI extensions.
- **RAR (RFC 9396) policy enforcement**: every call is checked against a
  policy before the backend is contacted.
- **Operation classification** by effect (read-only, idempotent write, action)
  and sensitivity.
- **Transports**: stdio and streamable HTTP.
- **Inbound OAuth** for the HTTP transport: JWT validation, JWKS, and
  RFC 9728 discovery.
- **Outbound credentials**: static, `client_credentials`, and RFC 8693
  `token_exchange`.
- **Secret handling**: secrets are `${env:}`/`${file:}` references, never
  literals, and sensitive headers can't be set from tool arguments.
- **Response size capping** with an explicit truncation marker.
- **CLI**: `validate` to check a config, `serve` to run the gateway, and
  `configure` to build or update a config and policy interactively.
- **JSON Schema** for the config file.
- **Docker images** and mock backends for integration testing.

[0.1.0]: https://github.com/JohnLKramer/mcp-sidekit/releases/tag/v0.1.0
