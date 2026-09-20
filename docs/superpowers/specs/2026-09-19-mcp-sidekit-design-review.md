# Review: mcp-portal Design Spec

**Date:** 2026-09-19
**Reviewed document:** [`2026-09-19-mcp-sidekit-design.md`](./2026-09-19-mcp-sidekit-design.md)
**Reviewer:** Claude (Senior Code Reviewer subagent, dispatched via `superpowers:requesting-code-review`)
**Verdict:** Ready to proceed to implementation planning — **with fixes**

Line references below point into the design spec.

---

## Strengths

- **Two-axis risk model** (`:156-181`). The `effect` × `sensitivity` split is the right shape; the `GET /users/{id}/ssn` example makes the case concretely, and the "Stated limitation" paragraph (`:176-180`) is honest that `introspect-safe` protects against method-level mistakes only.
- **"Mode controls exposure. Policy controls authorization."** (`:60-65`) is stated once and then actually threaded through the design — registry takes mode as an input (`:109-111`), policy runs in every mode (`:64-65`).
- **Policy semantics are argued, not asserted**: accumulation over first-match (`:361-365`), single-detail coverage (`:510-513`), exact location matching with the `pay`/`payments` escalation example (`:515-519`). Each has a rationale in §14 that a future maintainer can re-evaluate.
- **Retry gated on `effect`** (`:558-561`) is a genuinely good use of the classification and closes a real double-charge hazard.
- **Secrets enforced by schema** (`:324-332`) and **redaction at the logging boundary** (`:568-572`) both make the safe behavior structural rather than disciplinary.
- **Separate policy file with a separate owner and cadence** (`:353-359`). "The upstream does not get to decide what authority it requires" is exactly the right framing.
- **Honest stdio framing** (`:471-475`): "guardrail, not a security boundary," with a mandate that documentation use those words.
- **Configuration errors at startup, never at call time** (`:563-564`); **survey proposes rather than interrogates** (`:415-424`); **reconcile never deletes silently** (`:439`).
- **Testing plan** (§11) matches the architecture: the subtle logic is pure and I/O-free, and transport/auth tests use real in-process servers.

---

## Issues

### Critical (Must Fix)

#### 1. Argument binding specifies no encoding and no header denylist
**Where:** `:531`, `:393-400`

Step 6 of invocation says "bind arguments to an HTTP request using the binding's parameter locations" and nothing more. Built as written:

- A path param `id = "../admin/reset"` on `GET /v1/invoices/{id}` reaches an operation the registry never exposed, defeating Goal 3 entirely.
- Header params flattened into the tool schema (both OpenAPI docs and explicit config can declare them) let the model set `Authorization` (bypassing the outbound broker), `Host`, `Cookie`, `Content-Length`, `Transfer-Encoding`, or `X-Forwarded-*`.

**Fix:**
- Path params are strictly percent-encoded, with `/`, `?`, `#` always encoded, no exceptions.
- A fixed denylist of header names (`Authorization`, `Host`, `Cookie`, `Content-Length`, `Transfer-Encoding`, `Connection`, `Proxy-*`, plus whatever header the outbound mode writes) is rejected at load time for both introspected and explicit bindings.
- Add these as security regression tests in §11.

#### 2. RAR coverage silently ignores unknown fields and does not define empty arrays
**Where:** `:499-508`, `:344-348`

Coverage rules 1–4 check only `type`, `actions`, `locations`, `datatypes`. A policy author who writes `identifier: acct-123` or `privileges: [admin]` in a `require` block (both common RFC 9396 fields) believes it is enforced; it is not, and the rule is broader than written. This is fail-open in the one file that must fail closed. Likewise `actions: []` is `∅ ⊆ anything` and requires nothing.

**Fix:**
- The policy Pydantic model for `require.authorization_details[]` uses `extra="forbid"`.
- Define coverage for `identifier` (exact equality) and `privileges` (subset), or reject them.
- Reject empty arrays in `require` at load time.
- State that a presented detail lacking a field `R` requires — or a malformed `authorization_details` claim — fails coverage.

#### 3. HTTP transport with `auth.inbound.enabled: false` has no guard
**Where:** `:250`, `:227`

The example implies `enabled` can be `false`. Built as written, `transport: http` + `enabled: false` + `host: 0.0.0.0` is an unauthenticated network endpoint holding a service credential (`client_credentials` or `static`) that will call the upstream for anyone. This is a larger blast radius than `introspect-unsafe`, which the spec guards with a banner (`:54-56`), yet it has no equivalent. It is also undefined which principal is used on HTTP when inbound is disabled — §8 (`:460-462`) only covers stdio.

**Fix:** `inbound.enabled: false` with `transport: http` is a startup error unless `host` is loopback or an explicit `allow_unauthenticated_http: true` acknowledgment is present. State that the local principal is used in that case and log the same banner as `introspect-unsafe`.

### Important (Should Fix)

#### 4. P1 cannot deliver what it claims given the module table
**Where:** `:601`

P1 promises "explicit source, HTTP transport, stdio server ... static credentials," but:
- `naming.py` and `registry.py` — which produce the `ToolSet` the server resolves against (`:93`, `:525`) — are in P2.
- Explicit entries derive `name` from the naming strategy (`:302-303`), which is P2.
- `static` outbound lives in `auth/outbound.py` (`:97`), which is P4.
- `configured` mode is listed in P2, yet P1 must serve in some mode.

**Fix:** Either move minimal `naming.py`/`registry.py` (no mode posture, no selection) and the `none`/`static` outbound modes into P1 and say `configured` is the only P1 mode, or make P1 "library + tests only."

#### 5. Sensitivity "set via a policy match" contradicts the architecture
**Where:** `:177`

Sensitivity drives exposure at registry-build time (`:173-174`). The policy file is evaluated at invocation (`:529`), after the `ToolSet` exists (`:74`), and `rules[].require` has no sensitivity setter (`:336-351`). It also makes `match.sensitivity` (`:367`) circular.

**Fix:** Drop "via a policy match." Sensitivity comes from config `operations[]`, `x-mcp-sensitive`, or a new `classification` override block in the main config.

#### 6. Selection and policy match on generated tool names, which are unstable by design
**Where:** `:261-262`, `:319-320`, `:367`, `:198-201`

Names change with `naming.strategy`, prefixes, truncation, and collision hash suffixes. A collision renames `foo` to `foo_a1b2`, and `exclude_names: ["foo"]` or a `match.name: foo` policy rule silently stops applying — fail-open in both places. The pipeline (`:74`) also never says whether naming runs before or after selection; if after, removing one of two colliding operations changes the survivor's name.

**Fix:** Selection and policy `match` key on the stable `id` (with `name` allowed but documented as fragile). Naming runs after selection. Startup emits a warning (or fails under a `strict` flag) for any rule or selection glob that matches zero operations.

#### 7. "Adding a rule can only tighten" is false under `unmatched: deny`
**Where:** `:361-372`

A rule with no `require` (e.g. one written only for `outbound.carry`) matches the operation, so the operation is no longer "unmatched" and becomes callable with no requirements. Adding that rule loosened policy.

**Fix:** Define that a rule contributes to "matched" only if it has a non-empty `require`, or reject `require`-less rules under `deny`. State whichever is chosen.

#### 8. `carry: true` is not defined
**Where:** `:349-350`, `:493`

Is what's carried the rule's required detail `R`, the presented detail `p` that covered it, or the caller's entire `authorization_details`? These differ in authority (least privilege vs. full delegation) and in the cache key (`:495`). Also unspecified:
- `carry` on `client_credentials` (RFC 9396 allows `authorization_details` there too).
- `token_exchange` config fields (`audience`/`resource`, `requested_token_type`, client auth).
- `token_exchange` under stdio has no `subject_token` and must be a startup error per `:563`.

#### 9. Token-exchange cache key omits the subject token
**Where:** `:495-497`

Keyed on subject + scopes + hash(details), two different inbound tokens for the same `sub` (different sessions, one since revoked, different `exp`) share one exchanged token, and the exchanged token can outlive the inbound token it was derived from.

**Fix:** For `token_exchange`, include a hash of the subject token (or its `jti`) in the key and cap cache TTL at `min(exchanged.exp, subject.exp)`. For `client_credentials`, including the principal subject buys nothing and multiplies IdP calls by user count; drop it unless details are carried.

#### 10. Inbound JWT validation is missing standard controls
**Where:** `:482-484`

Missing: `alg` allowlist (reject `none`/HS*), `typ` handling (`at+jwt`, RFC 9068), clock-skew leeway, JWKS refresh-on-unknown-`kid` with rate limit, how `jwks_uri` is found (explicit vs. discovery from `issuer`), `scope` claim format (space-delimited string vs. `scp` array), `aud`-as-array. Opaque tokens/introspection, DPoP, and mTLS should be listed as out of scope.

#### 11. Streamable HTTP is underspecified for a real client to connect
**Where:** `:227`, `:479-481`

No endpoint path, no `Origin` validation (the MCP spec requires it against DNS rebinding), no session (`Mcp-Session-Id`) statement, no SSE/GET decision. RFC 9728 metadata content is unstated: `resource` must equal `auth.inbound.audience`, and the path-suffixed well-known variant applies when the resource has a path (`/mcp`).

#### 12. Request body and flattening details are underspecified for the explicit source (P1)
**Where:** `:290-299`, `:393-400`

`binding.parameters[].in` shows `query` only; how is a body declared? Are body top-level properties flattened or is the body one argument? What about non-object bodies, `required` merging, and content types other than JSON (form, multipart)? The collision rule prefixes only colliders, so a body property literally named `query_id` re-collides; specify the fallback (error at load). `HttpBinding` needs an explicit arg-name → (location, original-name, style/explode) mapping to be genuinely "reversible."

#### 13. OpenAPI handling gaps a plan author must guess at
**Where:** `:378-380`

- `servers[]` vs `base_url` precedence (server URLs often carry a `/v1` prefix).
- `security` requirements — ignored, presumably; say so.
- Path-level `parameters`; `deprecated`.
- External `$ref` — fetching remote refs is an SSRF vector; disable or allowlist.
- Circular refs in a self-contained `input_schema`.
- `x-mcp-exclude` vs `introspect-unsafe`, and whether config can re-include.
- Whether `operations[]` merge into introspect modes (registry "dedupe" at `:93` suggests yes).
- Fetch-once-at-startup vs refresh.

#### 14. Response mapping and retry have no numbers
**Where:** `:535-541`, `:555`, `:558`

Response: size cap default, non-JSON text handling, whether `structuredContent`/`outputSchema` is emitted, whether 4xx sets `isError`.
Retry: count, backoff, jitter, which codes (502/503/504; 429 with `Retry-After`?), total budget vs `timeout_ms`.

#### 15. `static` mode lacks header name/scheme fields
**Where:** `:491`

`Authorization: Bearer` vs `X-API-Key` is undefined, and this mode is in P1.

#### 16. "Cannot be reached by default or by omission" has no stated mechanism
**Where:** `:54`

Say it: `mode` is required in the schema with no default, the CLI has no `--mode` default, and optionally `introspect-unsafe` requires a second explicit acknowledgment.

### Minor (Nice to Have)

17. `:51` — `configure` is in the mode table but is "not a server mode"; the schema's `mode` enum should exclude it, and the spec should say so.
18. `:198` — Regex permits `A-Z` while names are "lowercased"; clarify the regex is for override validation. Some clients cap tool names at 64 chars; with upstream + tag prefixes and a hash suffix, 128 is optimistic. Make the max configurable, default 64.
19. `:264` — `prefix_with_group_tag` with multiple or zero tags: which tag? Define (first tag in document order; none means no prefix).
20. `:163-167` — `openWorldHint` and `title` are not addressed; state the values.
21. `:165` — HEAD and OPTIONS as tools are noise; exclude by default.
22. `:317` — `include_tags` with a multi-tag operation: any-match or all-match?
23. `:220`, `:271` — Placeholder `$schema` URL and no migration story for `version`; state that unknown versions are load errors.
24. `:441` — "Byte-identical, comments intact" requires a round-trip YAML library and is impossible for JSON configs; note it.
25. `:265`, `:332` — Relative-path resolution for `policy.file` and `${file:}` (relative to the config file, presumably); strip trailing newline from file secrets.
26. `:566` — Observability is logging only; no metrics, tracing, or request-id propagation to the upstream.

---

## Recommendations

- Resolve Critical 1–3 and Important 4–9 in the spec before planning; the rest can be resolved as decisions recorded in the plan.
- Add a short **"Request construction"** subsection to §9 covering encoding, header denylist, serialization style, and body handling. That one section closes issues 1, 12, and 15.
- Add a **"Startup validation"** list to §10 enumerating the config combinations that are load errors: HTTP + inbound disabled + non-loopback; `token_exchange` + stdio; dead policy rules under `strict`; denylisted header params.
- Key policy `match` and `selection` on stable `id` as the primary key; this is what actually delivers Goal 4 ("durable against regenerated API descriptions").

---

## Assessment

**Ready to proceed to implementation planning?** With fixes.

**Reasoning:** The architecture, module boundaries, and the security-relevant decisions that *are* made are sound and well-argued. But request-binding safety, unauthenticated HTTP, and strict RAR parsing are unspecified in ways that would ship holes if built literally, and P1 as phased depends on modules from P2 and P4. Those are spec-author decisions, not planner decisions.
