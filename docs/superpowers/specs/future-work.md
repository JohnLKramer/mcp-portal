# Future Work

High-level tracking for ideas that don't have a design yet — one short
entry per idea, no design. See the main design spec's §13 (Deferred) for
already-tracked items with rationale (gRPC, DASH/HLS/QUIC, hierarchical RAR
location matching, opaque-token introspection/DPoP/mTLS, SSE
streaming/MCP sessions, `structuredContent`/`outputSchema`, form/multipart
bodies, runtime introspection refresh, metrics/tracing, MCP
resources/prompts) — this file doesn't duplicate those, only adds what
isn't there yet.

## Web resource crawling for allow/deny-listing

Crawl target sites to help generate a whitelist or blacklist of exposable
endpoints/resources, for `x-mcp-exclude`-style curation. Priority not yet
set. Includes a sub-note on quick-and-dirty text summarization of crawled
pages, as a likely helper for this same feature rather than an independent
capability.
