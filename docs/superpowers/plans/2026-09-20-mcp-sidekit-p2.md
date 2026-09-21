# mcp-portal P2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Point mcp-portal at an OpenAPI document and serve its operations as MCP
tools, with two new modes — `introspect-safe` and `introspect-unsafe` — added
alongside P1's `configured` mode.

**Architecture:** A new `OperationSource` (`sources/openapi.py`) that produces the
same `Operation` model P1's `ExplicitSource` produces, so `registry.py`,
`naming.py`, and the server never learn a new code path. Getting from a raw
OpenAPI document to that model is four small, pure, independently testable
steps — fetch/parse, resolve `$ref`, convert the 3.0 dialect, extract
per-operation records — composed by `OpenApiSource`. Introspected and explicit
operations are merged by `id` before `registry.build_toolset` ever sees them, so
mode-based exposure posture is one function (`apply_mode_posture`) inserted
between classification and selection.

**Tech Stack:** Same as P1 (Python 3.14.7, uv, Pydantic v2, httpx, `mcp` SDK,
ruamel.yaml, jsonschema, pytest, ruff, mypy). No new dependencies: document
fetch reuses `httpx`, document parsing reuses `ruamel.yaml`/`json`.

**Spec:** [`docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md`](../specs/2026-09-19-mcp-sidekit-design.md)

## Global Constraints

- **Scope is P2 only.** Not in this plan: `config/policy.py`, `auth/rar.py`,
  `policy.py`, `auth/inbound.py`, `auth/outbound.py`'s `client_credentials` /
  `token_exchange` modes, `server/http.py`, and `configure/`. Do not add config
  fields for them.
- **`mode`'s enum grows to exactly** `configured`, `introspect-safe`,
  `introspect-unsafe`. Still required, still no default (P1 constraint holds).
- **`introspect-unsafe` additionally requires `acknowledge_unsafe: true`.**
  Selecting the mode alone must never be sufficient — this is a Pydantic
  validator, not a CLI flag or a runtime check, so it cannot be reached by
  omission.
- **`UpstreamConfig.base_url` becomes optional.** `base_url` wins when set;
  otherwise it is resolved from the OpenAPI document's `servers[]` at load time.
  **`configured` mode still requires every upstream to declare `base_url`
  explicitly**, since it never introspects and so could never resolve it any
  other way — this is caught by a Pydantic validator, not left to fail at the
  first tool call.
- **`application/json` is still the only supported request body content type.**
  For the *explicit* source this remains a hard config error (P1 behavior,
  unchanged). For the *introspected* source, an operation whose only content
  type is unsupported is **skipped with a warning**, per §6 of the design — the
  document is third-party input, not something an operator hand-wrote, so one
  bad operation must not fail the whole load.
- **External `$ref` is disabled by default.** A document controls its own
  `$ref` targets, so honoring them unconditionally is an SSRF (network) and
  path-traversal (file) vector. Enabling it requires
  `introspection.openapi.allow_external_refs: true` **and** a non-empty
  `introspection.openapi.allowed_hosts` naming every host a networked ref may
  target.
- **Circular `$ref` is detected and replaced with `{"type": "object"}`**, with a
  logged warning naming the ref. An MCP `input_schema` must be finite.
- **`x-mcp-exclude` is honored in every mode**, including `introspect-unsafe`.
  Mode governs risk posture, not upstream opt-outs.
- **Deprecated operations are excluded by default**, included only with
  `introspection.openapi.include_deprecated: true`.
- **HEAD and OPTIONS are still never exposed.** Reused unchanged from
  `classify.EXPOSED_METHODS`.
- **OpenAPI parameter `style` is not honored from the document.** Every
  parameter is treated as `style: "form"` — the only style
  `transports/http.py` implements, and the same limitation P1 already accepts
  for explicit `operations[]` entries (`ParameterEntry.style` is
  `Literal["form"]`).
- **The document is fetched once at startup.** No runtime refresh. Restarting,
  or a future `configure`, is what re-introspection is for.
- **`operations.py`, `flatten.py`, and `classify.py` are unchanged.** P2 is a
  new producer of `Operation`s, not a new shape of `Operation`.
- Every task ends with `uv run ruff format .`, `uv run ruff check .`,
  `uv run mypy src`, and the task's own test file passing before the commit
  step. Do not run the full suite mid-task; Task 11 is where everything is run
  together.

---

### Task 1: Config models for introspection and the unsafe-mode gate

**Files:**
- Modify: `src/mcp_portal/config/models.py`
- Modify: `tests/test_config_models.py`
- Modify: `schema/config-v1.schema.json` (regenerated, not hand-edited)

**Interfaces:**
- Consumes: `BaseUrl` (existing), nothing new from other P2 modules.
- Produces: `OpenApiIntrospectionConfig`, `IntrospectionConfig`,
  `UpstreamConfig.introspection: IntrospectionConfig | None`,
  `UpstreamConfig.base_url: BaseUrl | None`, `Config.mode` extended to
  `Literal["configured", "introspect-safe", "introspect-unsafe"]`,
  `Config.acknowledge_unsafe: bool`.

- [ ] **Step 1: Update the existing test that will now be wrong**

`test_p1_mode_enum_contains_only_configured` currently asserts that
`mode: introspect-safe` is *rejected*. P2 makes it valid, so this test's
premise is gone. Replace it in `tests/test_config_models.py`:

```python
def test_p1_mode_enum_contains_only_configured():
    payload = MINIMAL | {"mode": "introspect-safe"}
    with pytest.raises(ValidationError):
        Config.model_validate(payload)
```

with:

```python
def test_mode_enum_rejects_anything_outside_the_three_published_values():
    payload = MINIMAL | {"mode": "surveil"}
    with pytest.raises(ValidationError):
        Config.model_validate(payload)
```

- [ ] **Step 2: Write the new failing tests**

Append to `tests/test_config_models.py`:

```python
def test_introspect_safe_does_not_require_acknowledgement():
    Config.model_validate(MINIMAL | {"mode": "introspect-safe"})


def test_introspect_unsafe_without_acknowledgement_is_rejected():
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(MINIMAL | {"mode": "introspect-unsafe"})
    assert "acknowledge_unsafe" in str(exc.value)


def test_introspect_unsafe_with_acknowledgement_is_accepted():
    cfg = Config.model_validate(
        MINIMAL | {"mode": "introspect-unsafe", "acknowledge_unsafe": True}
    )
    assert cfg.mode == "introspect-unsafe"


def test_upstream_with_neither_base_url_nor_introspection_is_rejected():
    payload = MINIMAL | {"mode": "introspect-safe", "upstreams": {"billing": {}}}
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "base_url" in str(exc.value) or "introspection" in str(exc.value)


def test_upstream_may_rely_on_introspection_instead_of_base_url():
    payload = MINIMAL | {
        "mode": "introspect-safe",
        "upstreams": {
            "billing": {
                "introspection": {
                    "openapi": {"url": "https://api.example.com/openapi.json"}
                }
            }
        },
    }
    cfg = Config.model_validate(payload)
    assert cfg.upstreams["billing"].base_url is None
    assert cfg.upstreams["billing"].introspection.openapi.url == (
        "https://api.example.com/openapi.json"
    )


def test_configured_mode_requires_base_url_even_when_introspection_is_present():
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "introspection": {
                    "openapi": {"url": "https://api.example.com/openapi.json"}
                }
            }
        }
    }
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "configured" in str(exc.value)


def test_openapi_introspection_requires_exactly_one_of_url_or_file():
    def with_openapi(openapi: dict) -> dict:
        return MINIMAL | {
            "mode": "introspect-safe",
            "upstreams": {
                "billing": {"base_url": "https://api.example.com", "introspection": {"openapi": openapi}}
            },
        }

    with pytest.raises(ValidationError):
        Config.model_validate(with_openapi({}))
    with pytest.raises(ValidationError):
        Config.model_validate(
            with_openapi(
                {"url": "https://api.example.com/openapi.json", "file": "./openapi.json"}
            )
        )


def test_openapi_introspection_defaults():
    payload = MINIMAL | {
        "mode": "introspect-safe",
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "introspection": {"openapi": {"file": "./openapi.json"}},
            }
        },
    }
    openapi = Config.model_validate(payload).upstreams["billing"].introspection.openapi
    assert openapi.include_deprecated is False
    assert openapi.allow_external_refs is False
    assert openapi.allowed_hosts == []
```

- [ ] **Step 3: Run the tests to verify the new ones fail**

Run: `uv run pytest tests/test_config_models.py -v`
Expected: the six new tests FAIL — `AttributeError` / `ValidationError` not
raised, since `introspection`, `acknowledge_unsafe`, and the new mode values
do not exist yet. The rewritten `test_mode_enum_rejects_anything_outside_the_three_published_values`
passes already (an unknown literal is already rejected).

- [ ] **Step 4: Write the implementation**

In `src/mcp_portal/config/models.py`, add after `BaseUrl`:

```python
class OpenApiIntrospectionConfig(Base):
    url: BaseUrl | None = None
    file: str | None = None
    include_deprecated: bool = False
    allow_external_refs: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_document_source(self) -> Self:
        if (self.url is None) == (self.file is None):
            raise ValueError(
                "introspection.openapi requires exactly one of 'url' or 'file'"
            )
        return self


class IntrospectionConfig(Base):
    openapi: OpenApiIntrospectionConfig
```

Modify `UpstreamConfig`:

```python
class UpstreamConfig(Base):
    protocol: Literal["http"] = "http"
    base_url: BaseUrl | None = None
    timeout_ms: int = Field(default=30000, gt=0)
    max_total_ms: int | None = Field(default=None, gt=0)
    max_response_bytes: int = Field(default=1024 * 1024, gt=0)
    auth: UpstreamAuthConfig = Field(default_factory=UpstreamAuthConfig)
    introspection: IntrospectionConfig | None = None

    @model_validator(mode="after")
    def _default_total_budget(self) -> Self:
        if self.max_total_ms is None:
            object.__setattr__(self, "max_total_ms", self.timeout_ms * 3)
        return self

    @model_validator(mode="after")
    def _base_url_or_introspection(self) -> Self:
        if self.base_url is None and self.introspection is None:
            raise ValueError(
                "upstream requires either 'base_url' or 'introspection.openapi': "
                "there is otherwise no way to determine which server to call"
            )
        return self
```

Modify `Config`:

```python
class Config(Base):
    version: Literal["1"]
    mode: Literal["configured", "introspect-safe", "introspect-unsafe"]
    acknowledge_unsafe: bool = False
    server: ServerConfig
    upstreams: dict[str, UpstreamConfig]
    operations: list[OperationEntry] = Field(default_factory=list)
    classification: list[ClassificationRule] = Field(default_factory=list)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    naming: NamingConfig = Field(default_factory=NamingConfig)

    @model_validator(mode="after")
    def _operations_reference_known_upstreams(self) -> Self:
        unknown = sorted({o.upstream for o in self.operations} - set(self.upstreams))
        if unknown:
            raise ValueError(f"operations reference unknown upstreams: {unknown}")
        return self

    @model_validator(mode="after")
    def _unsafe_mode_requires_acknowledgement(self) -> Self:
        if self.mode == "introspect-unsafe" and not self.acknowledge_unsafe:
            raise ValueError(
                "mode 'introspect-unsafe' requires 'acknowledge_unsafe: true'; selecting "
                "the mode alone must not be enough to expose every discovered operation"
            )
        return self

    @model_validator(mode="after")
    def _configured_mode_needs_every_base_url(self) -> Self:
        if self.mode == "configured":
            missing = sorted(k for k, u in self.upstreams.items() if u.base_url is None)
            if missing:
                raise ValueError(
                    f"upstream(s) {missing} have no 'base_url'; mode 'configured' never "
                    "introspects, so it can never be resolved from a document"
                )
        return self
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config_models.py -v`
Expected: all passed.

- [ ] **Step 6: Regenerate the schema and verify the drift check**

```bash
uv run python -m mcp_portal.config.schema
uv run pytest tests/test_schema_drift.py -v
```
Expected: schema rewritten; 2 passed.

- [ ] **Step 7: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 8: Commit**

```bash
git add src/mcp_portal/config/models.py tests/test_config_models.py schema/config-v1.schema.json
git commit -m "feat: add introspection config, unsafe-mode gate, optional base_url"
```

---

### Task 2: OpenAPI document loading and server URL resolution

**Files:**
- Create: `src/mcp_portal/sources/openapi_document.py`
- Create: `tests/test_openapi_document.py`

**Interfaces:**
- Consumes: nothing from other P2 tasks.
- Produces: `OpenApiError`, `parse_document(text, *, is_yaml) -> dict`,
  `fetch_text(location, base_dir, client) -> tuple[str, bool]`,
  `resolve_base_url(document, base_url_override) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_openapi_document.py`:

```python
from pathlib import Path

import httpx
import pytest

from mcp_portal.sources.openapi_document import (
    OpenApiError,
    fetch_text,
    parse_document,
    resolve_base_url,
)

MINIMAL_JSON = '{"openapi": "3.1.0", "info": {"title": "x", "version": "1"}}'
MINIMAL_YAML = "openapi: '3.1.0'\ninfo: {title: x, version: '1'}\n"


def test_parse_document_reads_json():
    doc = parse_document(MINIMAL_JSON, is_yaml=False)
    assert doc["openapi"] == "3.1.0"


def test_parse_document_reads_yaml():
    doc = parse_document(MINIMAL_YAML, is_yaml=True)
    assert doc["openapi"] == "3.1.0"


def test_parse_document_rejects_invalid_json():
    with pytest.raises(OpenApiError):
        parse_document("{not json", is_yaml=False)


def test_parse_document_rejects_a_document_with_no_openapi_field():
    with pytest.raises(OpenApiError) as exc:
        parse_document("{}", is_yaml=False)
    assert "openapi" in str(exc.value)


def test_fetch_text_reads_a_local_file_relative_to_base_dir(tmp_path: Path):
    (tmp_path / "openapi.json").write_text(MINIMAL_JSON)
    text, is_yaml = fetch_text("openapi.json", tmp_path, httpx.Client())
    assert text == MINIMAL_JSON
    assert is_yaml is False


def test_fetch_text_reads_a_yaml_file_by_extension(tmp_path: Path):
    (tmp_path / "openapi.yaml").write_text(MINIMAL_YAML)
    _, is_yaml = fetch_text("openapi.yaml", tmp_path, httpx.Client())
    assert is_yaml is True


def test_fetch_text_missing_file_is_an_openapi_error(tmp_path: Path):
    with pytest.raises(OpenApiError):
        fetch_text("nope.json", tmp_path, httpx.Client())


def test_fetch_text_fetches_a_url(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=MINIMAL_JSON, headers={"content-type": "application/json"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    text, is_yaml = fetch_text("https://api.example.com/openapi.json", tmp_path, client)
    assert text == MINIMAL_JSON
    assert is_yaml is False


def test_fetch_text_surfaces_an_http_error(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(OpenApiError):
        fetch_text("https://api.example.com/openapi.json", tmp_path, client)


def test_base_url_override_always_wins():
    doc = {"servers": [{"url": "https://from-document.example.com"}]}
    assert resolve_base_url(doc, "https://override.example.com") == "https://override.example.com"


def test_base_url_falls_back_to_the_first_server():
    doc = {"servers": [{"url": "https://first.example.com"}, {"url": "https://second.example.com"}]}
    assert resolve_base_url(doc, None) == "https://first.example.com"


def test_server_variables_substitute_their_default():
    doc = {
        "servers": [
            {
                "url": "https://{env}.example.com/{version}",
                "variables": {"env": {"default": "prod"}, "version": {"default": "v1"}},
            }
        ]
    }
    assert resolve_base_url(doc, None) == "https://prod.example.com/v1"


def test_a_server_variable_with_no_default_is_an_error():
    doc = {"servers": [{"url": "https://{env}.example.com", "variables": {"env": {}}}]}
    with pytest.raises(OpenApiError) as exc:
        resolve_base_url(doc, None)
    assert "env" in str(exc.value)


def test_no_override_and_no_servers_is_an_error():
    with pytest.raises(OpenApiError):
        resolve_base_url({}, None)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_openapi_document.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.sources.openapi_document'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/sources/openapi_document.py`:

```python
"""Fetch and parse an OpenAPI document, and resolve its base URL.

The document is fetched once at startup (§6 of the design); there is no
runtime refresh. A tool surface that changes underneath a connected client is
a correctness problem, not a feature.
"""

import json
from pathlib import Path
from typing import Any

import httpx
from ruamel.yaml import YAML


class OpenApiError(Exception):
    """Raised for any problem loading, parsing, or resolving an OpenAPI document."""


def parse_document(text: str, *, is_yaml: bool) -> dict[str, Any]:
    if is_yaml:
        data = YAML(typ="safe").load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OpenApiError(f"invalid JSON in OpenAPI document: {exc}") from exc
    if not isinstance(data, dict) or "openapi" not in data:
        raise OpenApiError("not an OpenAPI document: missing top-level 'openapi' version field")
    return data


def fetch_text(location: str, base_dir: Path, client: httpx.Client) -> tuple[str, bool]:
    """Return `(text, is_yaml)` for a URL or a file path relative to `base_dir`."""
    if location.startswith(("http://", "https://")):
        try:
            response = client.get(location, timeout=30.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OpenApiError(f"cannot fetch OpenAPI document {location!r}: {exc}") from exc
        content_type = response.headers.get("content-type", "")
        is_yaml = "yaml" in content_type or location.endswith((".yaml", ".yml"))
        return response.text, is_yaml

    path = Path(location)
    resolved = path if path.is_absolute() else base_dir / path
    try:
        text = resolved.read_text()
    except OSError as exc:
        raise OpenApiError(f"cannot read OpenAPI document {resolved}: {exc}") from exc
    return text, resolved.suffix in {".yaml", ".yml"}


def resolve_base_url(document: dict[str, Any], base_url_override: str | None) -> str:
    """`base_url` wins when set (§6); otherwise the document's first declared server."""
    if base_url_override:
        return base_url_override

    servers = document.get("servers") or []
    if not servers:
        raise OpenApiError(
            "no 'base_url' was configured and the document declares no servers[] to fall "
            "back to"
        )

    url = servers[0].get("url", "")
    for name, spec in (servers[0].get("variables") or {}).items():
        default = spec.get("default")
        if default is None:
            raise OpenApiError(f"server variable {name!r} has no default and cannot be resolved")
        url = url.replace("{" + name + "}", str(default))
    return url
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_openapi_document.py -v`
Expected: 13 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/sources/openapi_document.py tests/test_openapi_document.py
git commit -m "feat: load OpenAPI documents and resolve their base URL"
```

---

### Task 3: `$ref` resolution with the SSRF/path-traversal guard

**Files:**
- Create: `src/mcp_portal/sources/refs.py`
- Create: `tests/test_refs.py`

**Interfaces:**
- Consumes: nothing from other P2 tasks (deliberately: kept pure and testable
  with a fake `fetch_external` callback rather than real I/O).
- Produces: `RefError`, `RefResolver(document, *, allow_external, allowed_hosts, fetch_external=None)`
  with `.resolve() -> dict` and `.warnings: list[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_refs.py`:

```python
import pytest

from mcp_portal.sources.refs import RefError, RefResolver


def test_internal_ref_resolves_to_its_target():
    doc = {
        "components": {"schemas": {"Amount": {"type": "integer"}}},
        "x": {"$ref": "#/components/schemas/Amount"},
    }
    resolved = RefResolver(doc, allow_external=False).resolve()
    assert resolved["x"] == {"type": "integer"}


def test_ref_nested_inside_a_list_resolves():
    doc = {
        "components": {"schemas": {"Amount": {"type": "integer"}}},
        "items": [{"$ref": "#/components/schemas/Amount"}, {"type": "string"}],
    }
    resolved = RefResolver(doc, allow_external=False).resolve()
    assert resolved["items"] == [{"type": "integer"}, {"type": "string"}]


def test_a_ref_target_that_itself_contains_a_ref_is_resolved_recursively():
    doc = {
        "components": {
            "schemas": {
                "Amount": {"type": "integer"},
                "Money": {"type": "object", "properties": {"amount": {"$ref": "#/components/schemas/Amount"}}},
            }
        },
        "x": {"$ref": "#/components/schemas/Money"},
    }
    resolved = RefResolver(doc, allow_external=False).resolve()
    assert resolved["x"]["properties"]["amount"] == {"type": "integer"}


def test_a_broken_pointer_is_a_ref_error():
    doc = {"x": {"$ref": "#/components/schemas/Missing"}}
    with pytest.raises(RefError):
        RefResolver(doc, allow_external=False).resolve()


def test_a_direct_self_reference_is_replaced_with_an_open_object_and_warns():
    # The cycle is only detectable once something *points at* Node — chain
    # tracking follows $ref expansion, not raw document position, so an
    # entry-point $ref is what puts "Node" on the chain before its own
    # self-reference is reached.
    doc = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Node"}},
                }
            }
        },
        "x": {"$ref": "#/components/schemas/Node"},
    }
    resolver = RefResolver(doc, allow_external=False)
    resolved = resolver.resolve()
    assert resolved["x"]["properties"]["child"] == {"type": "object"}
    assert any("circular" in w for w in resolver.warnings)


def test_a_networked_external_ref_is_rejected_by_default():
    doc = {"x": {"$ref": "https://evil.example.com/frag.yaml#/Foo"}}
    with pytest.raises(RefError) as exc:
        RefResolver(doc, allow_external=False).resolve()
    assert "external" in str(exc.value)


def test_a_networked_external_ref_outside_the_host_allowlist_is_rejected():
    doc = {"x": {"$ref": "https://evil.example.com/frag.yaml#/Foo"}}
    with pytest.raises(RefError):
        RefResolver(
            doc, allow_external=True, allowed_hosts=frozenset({"trusted.example.com"})
        ).resolve()


def test_a_networked_external_ref_within_the_allowlist_is_fetched():
    fetched = {"Foo": {"type": "integer"}}
    doc = {"x": {"$ref": "https://trusted.example.com/frag.yaml#/Foo"}}
    resolver = RefResolver(
        doc,
        allow_external=True,
        allowed_hosts=frozenset({"trusted.example.com"}),
        fetch_external=lambda target: fetched,
    )
    assert resolver.resolve()["x"] == {"type": "integer"}


def test_a_relative_file_external_ref_is_rejected_by_default():
    doc = {"x": {"$ref": "common.yaml#/Foo"}}
    with pytest.raises(RefError):
        RefResolver(doc, allow_external=False).resolve()


def test_a_relative_file_external_ref_is_permitted_with_a_fetcher_when_allowed():
    fetched = {"Foo": {"type": "string"}}
    doc = {"x": {"$ref": "common.yaml#/Foo"}}
    resolver = RefResolver(doc, allow_external=True, fetch_external=lambda target: fetched)
    assert resolver.resolve()["x"] == {"type": "string"}


def test_external_documents_are_fetched_once_and_cached():
    calls: list[str] = []

    def fetch(target: str) -> dict:
        calls.append(target)
        return {"Foo": {"type": "string"}, "Bar": {"type": "integer"}}

    doc = {
        "x": {"$ref": "common.yaml#/Foo"},
        "y": {"$ref": "common.yaml#/Bar"},
    }
    RefResolver(doc, allow_external=True, fetch_external=fetch).resolve()
    assert calls == ["common.yaml"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_refs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.sources.refs'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/sources/refs.py`:

```python
"""Resolve `$ref` pointers within an OpenAPI document.

Internal refs (`#/...`) are always resolved. External refs name a URL or file
the *document* controls, so honoring them unconditionally lets the document
dictate which URLs sidekit fetches or which files it reads — an SSRF and
path-traversal vector. They are refused unless the operator opts in with an
explicit host allowlist.
"""

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit


class RefError(Exception):
    """Raised for a `$ref` this document is not permitted to use, or a broken pointer."""


def _split(ref: str) -> tuple[str, str]:
    """Split a `$ref` into its (possibly empty) external target and JSON pointer."""
    target, _, pointer = ref.partition("#")
    return target, ("#" + pointer) if pointer else ""


def _pointer_lookup(root: Mapping[str, Any], pointer: str) -> Any:
    if pointer in ("", "#"):
        return root
    if not pointer.startswith("#/"):
        raise RefError(f"unsupported $ref pointer form: {pointer!r}")
    node: Any = root
    for raw in pointer[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        try:
            node = node[int(key)] if isinstance(node, list) else node[key]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise RefError(f"$ref pointer {pointer!r} does not resolve: {exc}") from exc
    return node


def _is_networked(target: str) -> bool:
    return bool(urlsplit(target).scheme)


class RefResolver:
    """Resolves every `$ref` in `document`, returning a ref-free copy."""

    def __init__(
        self,
        document: Mapping[str, Any],
        *,
        allow_external: bool,
        allowed_hosts: frozenset[str] = frozenset(),
        fetch_external: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> None:
        self._root = document
        self._allow_external = allow_external
        self._allowed_hosts = allowed_hosts
        self._fetch_external = fetch_external
        self._external_cache: dict[str, Mapping[str, Any]] = {}
        self.warnings: list[str] = []

    def resolve(self) -> dict[str, Any]:
        result = self._walk(self._root, chain=(), root=self._root)
        assert isinstance(result, dict)
        return result

    def _walk(self, node: Any, chain: tuple[str, ...], root: Mapping[str, Any]) -> Any:
        if isinstance(node, Mapping) and isinstance(node.get("$ref"), str):
            return self._resolve_ref(node["$ref"], chain, root)
        if isinstance(node, Mapping):
            return {k: self._walk(v, chain, root) for k, v in node.items()}
        if isinstance(node, list):
            return [self._walk(v, chain, root) for v in node]
        return node

    def _resolve_ref(self, ref: str, chain: tuple[str, ...], root: Mapping[str, Any]) -> Any:
        if ref in chain:
            self.warnings.append(f"circular $ref {ref!r} replaced with an open object schema")
            return {"type": "object"}

        target, pointer = _split(ref)
        # A bare `#/...` ref found while walking an externally-fetched document
        # must resolve against *that* document, not the top-level one — shared
        # component files conventionally cross-reference each other this way.
        new_root = self._external_document(target) if target else root
        node = _pointer_lookup(new_root, pointer or "#")
        return self._walk(node, chain + (ref,), new_root)

    def _external_document(self, target: str) -> Mapping[str, Any]:
        if not self._allow_external:
            raise RefError(
                f"external $ref target {target!r} is disabled by default: the document "
                "would otherwise dictate which URLs or files sidekit reads. Set "
                "introspection.openapi.allow_external_refs and .allowed_hosts to permit it"
            )
        if _is_networked(target):
            host = urlsplit(target).netloc
            if host not in self._allowed_hosts:
                raise RefError(
                    f"external $ref target {target!r} has host {host!r}, which is not in "
                    "introspection.openapi.allowed_hosts"
                )
        if target not in self._external_cache:
            if self._fetch_external is None:
                raise RefError(
                    f"external $ref target {target!r} cannot be fetched: no fetcher configured"
                )
            self._external_cache[target] = self._fetch_external(target)
        return self._external_cache[target]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_refs.py -v`
Expected: 11 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/sources/refs.py tests/test_refs.py
git commit -m "feat: resolve internal and allowlisted external \$ref, guard against SSRF"
```

---

### Task 4: OpenAPI 3.0 → JSON Schema 2020-12 dialect conversion

**Files:**
- Create: `src/mcp_portal/sources/dialect.py`
- Create: `tests/test_dialect.py`

**Interfaces:**
- Consumes: nothing from other P2 tasks.
- Produces: `is_openapi_31(version: str) -> bool`, `convert_30_schema(schema: Any) -> Any`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dialect.py`:

```python
from mcp_portal.sources.dialect import convert_30_schema, is_openapi_31


def test_openapi_31_is_recognized_by_prefix():
    assert is_openapi_31("3.1.0") is True
    assert is_openapi_31("3.1.1") is True
    assert is_openapi_31("3.0.3") is False


def test_nullable_true_widens_type_to_include_null():
    schema = convert_30_schema({"type": "string", "nullable": True})
    assert schema["type"] == ["null", "string"]
    assert "nullable" not in schema


def test_nullable_false_is_just_dropped():
    schema = convert_30_schema({"type": "string", "nullable": False})
    assert schema == {"type": "string"}


def test_boolean_exclusive_minimum_true_becomes_the_numeric_form():
    schema = convert_30_schema({"type": "integer", "minimum": 0, "exclusiveMinimum": True})
    assert schema == {"type": "integer", "exclusiveMinimum": 0}


def test_boolean_exclusive_minimum_false_keeps_minimum_inclusive():
    schema = convert_30_schema({"type": "integer", "minimum": 0, "exclusiveMinimum": False})
    assert schema == {"type": "integer", "minimum": 0}


def test_boolean_exclusive_maximum_true_becomes_the_numeric_form():
    schema = convert_30_schema({"type": "integer", "maximum": 10, "exclusiveMaximum": True})
    assert schema == {"type": "integer", "exclusiveMaximum": 10}


def test_singular_example_becomes_an_examples_array():
    schema = convert_30_schema({"type": "string", "example": "abc"})
    assert schema == {"type": "string", "examples": ["abc"]}


def test_conversion_recurses_into_nested_properties():
    schema = convert_30_schema(
        {
            "type": "object",
            "properties": {"amount": {"type": "integer", "nullable": True}},
        }
    )
    assert schema["properties"]["amount"]["type"] == ["integer", "null"]


def test_conversion_recurses_into_array_items():
    schema = convert_30_schema({"type": "array", "items": {"type": "string", "nullable": True}})
    assert schema["items"]["type"] == ["null", "string"]


def test_non_dict_non_list_values_pass_through_unchanged():
    assert convert_30_schema("not a schema") == "not a schema"
    assert convert_30_schema(42) == 42


def test_a_schema_with_none_of_the_30_isms_is_unchanged():
    schema = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
    assert convert_30_schema(schema) == schema
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_dialect.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.sources.dialect'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/sources/dialect.py`:

```python
"""Convert an OpenAPI 3.0 schema object to JSON Schema 2020-12.

OpenAPI 3.1 schemas *are* JSON Schema 2020-12 and pass through unchanged.
OpenAPI 3.0 uses a near-miss dialect — `nullable: true`, boolean
`exclusiveMinimum`/`exclusiveMaximum`, singular `example` — that produces a
subtly wrong tool schema if passed through untouched, in ways that surface
only at call time.
"""

from typing import Any


def is_openapi_31(version: str) -> bool:
    return version.startswith("3.1")


def convert_30_schema(schema: Any) -> Any:
    """Recursively convert one OpenAPI 3.0 schema object. Non-schema values pass through."""
    if isinstance(schema, list):
        return [convert_30_schema(v) for v in schema]
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("nullable", "exclusiveMinimum", "exclusiveMaximum") and isinstance(value, bool):
            continue
        if key == "example":
            out["examples"] = [convert_30_schema(value)]
            continue
        out[key] = convert_30_schema(value)

    if schema.get("nullable") is True and "type" in out:
        base = out["type"]
        types = base if isinstance(base, list) else [base]
        out["type"] = sorted({*types, "null"})

    if schema.get("exclusiveMinimum") is True and "minimum" in out:
        out["exclusiveMinimum"] = out.pop("minimum")
    if schema.get("exclusiveMaximum") is True and "maximum" in out:
        out["exclusiveMaximum"] = out.pop("maximum")

    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_dialect.py -v`
Expected: 11 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/sources/dialect.py tests/test_dialect.py
git commit -m "feat: convert OpenAPI 3.0 schema dialect to JSON Schema 2020-12"
```

---

### Task 5: Path and parameter extraction

**Files:**
- Create: `src/mcp_portal/sources/openapi_paths.py`
- Create: `tests/test_openapi_paths.py`

**Interfaces:**
- Consumes: `EXPOSED_METHODS` from `classify.py` (Task 3 of P1, unchanged).
- Produces: `RawParameter`, `RawOperation`, `default_operation_id(method, path) -> str`,
  `extract_raw_operations(document, *, include_deprecated=False) -> list[RawOperation]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_openapi_paths.py`:

```python
from mcp_portal.sources.openapi_paths import default_operation_id, extract_raw_operations

DOC: dict = {
    "openapi": "3.1.0",
    "paths": {
        "/invoices": {
            "parameters": [{"name": "customerId", "in": "query", "required": True, "schema": {"type": "string"}}],
            "get": {"operationId": "list_invoices", "tags": ["billing"], "responses": {}},
            "post": {
                "operationId": "create_invoice",
                "tags": ["billing"],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
                "responses": {},
            },
        },
        "/invoices/{id}": {
            "get": {
                "operationId": "get_invoice",
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {},
            },
            "delete": {"operationId": "cancel_invoice", "x-mcp-sensitive": True, "responses": {}},
        },
    },
}


def test_default_operation_id_slugs_method_and_path():
    assert default_operation_id("GET", "/v1/invoices/{id}") == "get_v1_invoices_id"


def test_every_exposed_method_on_every_path_is_extracted():
    ops = extract_raw_operations(DOC)
    assert {(o.method, o.path) for o in ops} == {
        ("get", "/invoices"),
        ("post", "/invoices"),
        ("get", "/invoices/{id}"),
        ("delete", "/invoices/{id}"),
    }


def test_head_and_options_are_never_extracted():
    doc = {
        "paths": {
            "/x": {
                "head": {"operationId": "probe", "responses": {}},
                "options": {"operationId": "cors", "responses": {}},
            }
        }
    }
    assert extract_raw_operations(doc) == []


def test_path_level_parameters_are_inherited():
    ops = {o.operation_id: o for o in extract_raw_operations(DOC)}
    names = {p.name for p in ops["list_invoices"].parameters}
    assert "customerId" in names


def test_operation_level_parameters_win_on_name_and_location_collision():
    doc = {
        "paths": {
            "/x/{id}": {
                "parameters": [{"name": "id", "in": "path", "required": False, "schema": {"type": "integer"}}],
                "get": {
                    "operationId": "get_x",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {},
                },
            }
        }
    }
    (op,) = extract_raw_operations(doc)
    (param,) = op.parameters
    assert param.required is True
    assert param.schema == {"type": "string"}


def test_cookie_parameters_are_dropped():
    doc = {
        "paths": {
            "/x": {
                "get": {
                    "operationId": "get_x",
                    "parameters": [{"name": "session", "in": "cookie", "schema": {"type": "string"}}],
                    "responses": {},
                }
            }
        }
    }
    (op,) = extract_raw_operations(doc)
    assert op.parameters == ()


def test_request_body_prefers_application_json_when_present():
    ops = {o.operation_id: o for o in extract_raw_operations(DOC)}
    body = ops["create_invoice"].request_body
    assert body is not None
    assert body["content_type"] == "application/json"


def test_an_operation_with_no_request_body_has_none():
    ops = {o.operation_id: o for o in extract_raw_operations(DOC)}
    assert ops["list_invoices"].request_body is None


def test_a_non_json_only_request_body_carries_its_actual_content_type():
    doc = {
        "paths": {
            "/x": {
                "post": {
                    "operationId": "upload",
                    "requestBody": {"content": {"multipart/form-data": {"schema": {"type": "object"}}}},
                    "responses": {},
                }
            }
        }
    }
    (op,) = extract_raw_operations(doc)
    assert op.request_body["content_type"] == "multipart/form-data"


def test_x_mcp_extensions_are_captured():
    ops = {o.operation_id: o for o in extract_raw_operations(DOC)}
    assert ops["cancel_invoice"].extensions == {"x-mcp-sensitive": True}


def test_deprecated_operations_are_excluded_by_default():
    doc = {
        "paths": {
            "/x": {"get": {"operationId": "old", "deprecated": True, "responses": {}}}
        }
    }
    assert extract_raw_operations(doc) == []


def test_deprecated_operations_are_included_when_asked():
    doc = {
        "paths": {
            "/x": {"get": {"operationId": "old", "deprecated": True, "responses": {}}}
        }
    }
    (op,) = extract_raw_operations(doc, include_deprecated=True)
    assert op.operation_id == "old"


def test_tags_summary_and_description_are_carried_through():
    doc = {
        "paths": {
            "/x": {
                "get": {
                    "operationId": "get_x",
                    "tags": ["billing", "admin"],
                    "summary": "Get X.",
                    "description": "Longer description.",
                    "responses": {},
                }
            }
        }
    }
    (op,) = extract_raw_operations(doc)
    assert op.tags == ("billing", "admin")
    assert op.summary == "Get X."
    assert op.description == "Longer description."


def test_operations_with_no_operation_id_have_none():
    doc = {"paths": {"/x": {"get": {"responses": {}}}}}
    (op,) = extract_raw_operations(doc)
    assert op.operation_id is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_openapi_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.sources.openapi_paths'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/sources/openapi_paths.py`:

```python
"""Walk an OpenAPI document's `paths` object into per-operation records.

Kept separate from binding construction (Task 6) so the parameter-merge and
filtering rules unique to OpenAPI are testable without touching
`operations.py`'s types at all.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mcp_portal.classify import EXPOSED_METHODS

_SLUG_INVALID = re.compile(r"[^a-z0-9]+")
_PARAM_LOCATIONS = frozenset({"path", "query", "header"})


def default_operation_id(method: str, path: str) -> str:
    """Stable identity for an operation with no `operationId` (§4 of the design)."""
    return _SLUG_INVALID.sub("_", f"{method}_{path}".lower()).strip("_")


@dataclass(frozen=True, slots=True)
class RawParameter:
    name: str
    location: str
    required: bool
    schema: Mapping[str, Any]
    explode: bool


@dataclass(frozen=True, slots=True)
class RawOperation:
    method: str
    path: str
    operation_id: str | None
    summary: str | None
    description: str | None
    tags: tuple[str, ...]
    parameters: tuple[RawParameter, ...]
    request_body: dict[str, Any] | None
    extensions: Mapping[str, Any]


def _raw_parameter(param: Mapping[str, Any]) -> RawParameter | None:
    location = param.get("in")
    if location not in _PARAM_LOCATIONS:
        return None  # "cookie" has no ParamLocation counterpart; never exposed.
    return RawParameter(
        name=param["name"],
        location=location,
        required=bool(param.get("required", False)),
        schema=param.get("schema", {"type": "string"}),
        explode=param.get("explode", True),
    )


def _merged_parameters(
    path_params: list[dict[str, Any]], op_params: list[dict[str, Any]]
) -> tuple[RawParameter, ...]:
    """Merge path-level and operation-level parameters; operation-level wins on
    a `(name, in)` collision, per OpenAPI's own precedence rule."""
    by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
    for param in path_params:
        by_key[(param.get("name"), param.get("in"))] = param
    for param in op_params:
        by_key[(param.get("name"), param.get("in"))] = param

    return tuple(p for raw in by_key.values() if (p := _raw_parameter(raw)) is not None)


def _request_body(op: Mapping[str, Any]) -> dict[str, Any] | None:
    body = op.get("requestBody")
    if not body:
        return None
    content = body.get("content", {})
    if not content:
        return None
    content_type = "application/json" if "application/json" in content else next(iter(content))
    return {"content_type": content_type, "schema": content[content_type].get("schema", {})}


def _extensions(op: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in op.items() if k.startswith("x-mcp-")}


def extract_raw_operations(
    document: Mapping[str, Any], *, include_deprecated: bool = False
) -> list[RawOperation]:
    operations: list[RawOperation] = []
    paths = document.get("paths", {})
    for path in sorted(paths):
        path_item = paths[path]
        path_params = path_item.get("parameters", [])
        for method in sorted(path_item):
            if method.upper() not in EXPOSED_METHODS:
                continue
            op = path_item[method]
            if op.get("deprecated") and not include_deprecated:
                continue
            operations.append(
                RawOperation(
                    method=method,
                    path=path,
                    operation_id=op.get("operationId"),
                    summary=op.get("summary"),
                    description=op.get("description"),
                    tags=tuple(op.get("tags", [])),
                    parameters=_merged_parameters(path_params, op.get("parameters", [])),
                    request_body=_request_body(op),
                    extensions=_extensions(op),
                )
            )
    return operations
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_openapi_paths.py -v`
Expected: 14 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/sources/openapi_paths.py tests/test_openapi_paths.py
git commit -m "feat: extract per-operation records from an OpenAPI paths object"
```

---

### Task 6: The OpenAPI operation source

**Files:**
- Create: `src/mcp_portal/sources/openapi.py`
- Create: `tests/test_source_openapi.py`

**Interfaces:**
- Consumes: `OpenApiIntrospectionConfig` (Task 1); `OpenApiError`, `fetch_text`,
  `parse_document`, `resolve_base_url` (Task 2); `RefResolver`, `RefError`
  (Task 3); `is_openapi_31`, `convert_30_schema` (Task 4); `RawOperation`,
  `extract_raw_operations` (Task 5); `effect_for_method`, `UnsupportedMethod`
  (P1 `classify.py`); `build_input_schema`, `check_path_template`,
  `resolve_arg_names`, `FlattenError` (P1 `flatten.py`); `NAME_PATTERN` (P1
  `naming.py`).
- Produces: `LoadedDocument`, `load_document(config, base_dir, base_url_override, client) -> LoadedDocument`,
  `OpenApiSource(upstream_key, loaded, *, include_deprecated)` with `.operations() -> Iterable[Operation]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_source_openapi.py`:

```python
import logging
from pathlib import Path

import httpx
import pytest

from mcp_portal.config.models import OpenApiIntrospectionConfig
from mcp_portal.operations import Effect, Sensitivity
from mcp_portal.sources.openapi import OpenApiSource, load_document
from mcp_portal.sources.refs import RefError

DOC = {
    "openapi": "3.1.0",
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/invoices": {
            "get": {
                "operationId": "list_invoices",
                "summary": "List invoices.",
                "tags": ["billing"],
                "responses": {},
            }
        },
        "/invoices/{id}": {
            "delete": {
                "operationId": "cancel_invoice",
                "summary": "Cancel an invoice.",
                "x-mcp-sensitive": True,
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {},
            }
        },
        "/invoices/{id}/audit": {
            "get": {
                "operationId": "get_audit",
                "summary": "Internal audit trail.",
                "x-mcp-exclude": True,
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {},
            }
        },
        "/invoices/upload": {
            "post": {
                "operationId": "bulk_upload",
                "summary": "Upload a batch file.",
                "requestBody": {"content": {"multipart/form-data": {"schema": {"type": "object"}}}},
                "responses": {},
            }
        },
    },
}


def write_doc(tmp_path: Path, doc: dict = DOC, name: str = "openapi.json") -> Path:
    import json

    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return path


def load(tmp_path: Path, doc: dict = DOC, **overrides) -> list:
    write_doc(tmp_path, doc)
    config = OpenApiIntrospectionConfig(file="openapi.json", **overrides)
    loaded = load_document(config, tmp_path, None, httpx.Client())
    return list(OpenApiSource("billing", loaded, include_deprecated=config.include_deprecated).operations())


def test_a_get_operation_becomes_a_read_only_operation(tmp_path: Path):
    ops = {o.id: o for o in load(tmp_path)}
    assert ops["list_invoices"].effect is Effect.READ_ONLY
    assert ops["list_invoices"].upstream == "billing"
    assert ops["list_invoices"].group_tags == ("billing",)


def test_x_mcp_sensitive_marks_the_operation_sensitive(tmp_path: Path):
    ops = {o.id: o for o in load(tmp_path)}
    assert ops["cancel_invoice"].sensitivity is Sensitivity.SENSITIVE


def test_x_mcp_exclude_drops_the_operation_in_every_mode(tmp_path: Path):
    ops = {o.id: o for o in load(tmp_path)}
    assert "get_audit" not in ops


def test_an_unsupported_content_type_is_skipped_with_a_warning_not_a_hard_failure(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        ops = {o.id: o for o in load(tmp_path)}
    assert "bulk_upload" not in ops
    assert "multipart/form-data" in caplog.text


def test_input_schema_is_synthesized_from_the_binding(tmp_path: Path):
    ops = {o.id: o for o in load(tmp_path)}
    schema = ops["cancel_invoice"].input_schema
    assert schema["properties"]["id"] == {"type": "string"}
    assert schema["required"] == ["id"]


def test_title_falls_back_from_summary_to_description_to_id(tmp_path: Path):
    ops = {o.id: o for o in load(tmp_path)}
    assert ops["list_invoices"].title == "List invoices."


def test_operations_with_no_operation_id_get_a_stable_slug(tmp_path: Path):
    doc = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {"/ping": {"get": {"summary": "Ping.", "responses": {}}}},
    }
    (op,) = load(tmp_path, doc)
    assert op.id == "get_ping"


def test_x_mcp_name_sets_the_tool_name_not_the_id(tmp_path: Path):
    doc = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/ping": {"get": {"operationId": "ping", "x-mcp-name": "health_check", "responses": {}}}
        },
    }
    (op,) = load(tmp_path, doc)
    assert op.id == "ping"
    assert op.name == "health_check"


def test_an_invalid_x_mcp_name_is_dropped_with_a_warning(tmp_path, caplog):
    doc = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/ping": {"get": {"operationId": "ping", "x-mcp-name": "Not Valid!", "responses": {}}}
        },
    }
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        (op,) = load(tmp_path, doc)
    assert op.name == ""
    assert "Not Valid!" in caplog.text


def test_load_document_resolves_base_url_from_servers_when_upstream_has_none(tmp_path: Path):
    write_doc(tmp_path)
    config = OpenApiIntrospectionConfig(file="openapi.json")
    loaded = load_document(config, tmp_path, None, httpx.Client())
    assert loaded.base_url == "https://api.example.com/v1"


def test_load_document_lets_configured_base_url_override_the_document(tmp_path: Path):
    write_doc(tmp_path)
    config = OpenApiIntrospectionConfig(file="openapi.json")
    loaded = load_document(config, tmp_path, "https://override.example.com", httpx.Client())
    assert loaded.base_url == "https://override.example.com"


def test_load_document_rejects_an_external_ref_by_default(tmp_path: Path):
    doc = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/x": {
                "post": {
                    "operationId": "x",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "https://evil.example.com/frag.yaml#/Foo"}
                            }
                        }
                    },
                    "responses": {},
                }
            }
        },
    }
    write_doc(tmp_path, doc)
    config = OpenApiIntrospectionConfig(file="openapi.json")
    with pytest.raises(RefError):
        load_document(config, tmp_path, None, httpx.Client())


def test_load_document_applies_dialect_conversion_only_to_30_documents(tmp_path: Path):
    doc = {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/x": {
                "post": {
                    "operationId": "x",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"amount": {"type": "integer", "nullable": True}},
                                }
                            }
                        }
                    },
                    "responses": {},
                }
            }
        },
    }
    write_doc(tmp_path, doc)
    config = OpenApiIntrospectionConfig(file="openapi.json")
    loaded = load_document(config, tmp_path, None, httpx.Client())
    (op,) = list(OpenApiSource("billing", loaded, include_deprecated=False).operations())
    assert op.input_schema["properties"]["amount"]["type"] == ["integer", "null"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_source_openapi.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.sources.openapi'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/sources/openapi.py`:

```python
"""Build Operations from an OpenAPI document.

Mirrors `sources/explicit.py`: the same flatten and classify pipeline produces
identical tool schemas for identical bindings, whether the binding came from
config or from introspection. The difference is failure mode — an explicit
entry that fails to flatten is an operator's mistake and a hard config error;
an introspected operation that fails to flatten is a third party's document
and is skipped with a warning instead (§6 of the design).
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from mcp_portal.classify import UnsupportedMethod, effect_for_method
from mcp_portal.config.models import OpenApiIntrospectionConfig
from mcp_portal.naming import NAME_PATTERN
from mcp_portal.operations import (
    BodySpec,
    HttpBinding,
    Operation,
    ParamLocation,
    Parameter,
    Sensitivity,
)
from mcp_portal.sources.dialect import convert_30_schema, is_openapi_31
from mcp_portal.sources.flatten import (
    FlattenError,
    build_input_schema,
    check_path_template,
    resolve_arg_names,
)
from mcp_portal.sources.openapi_document import (
    OpenApiError,
    fetch_text,
    parse_document,
    resolve_base_url,
)
from mcp_portal.sources.openapi_paths import RawOperation, default_operation_id, extract_raw_operations
from mcp_portal.sources.refs import RefResolver

log = logging.getLogger("mcp_portal")

_LOCATION = {
    "path": ParamLocation.PATH,
    "query": ParamLocation.QUERY,
    "header": ParamLocation.HEADER,
}


@dataclass(frozen=True, slots=True)
class LoadedDocument:
    document: dict[str, Any]
    base_url: str
    warnings: tuple[str, ...]


def _fetch_external(target: str, base_dir: Path, client: httpx.Client) -> dict[str, Any]:
    text, is_yaml = fetch_text(target, base_dir, client)
    return parse_document(text, is_yaml=is_yaml)


def load_document(
    config: OpenApiIntrospectionConfig,
    base_dir: Path,
    base_url_override: str | None,
    client: httpx.Client,
) -> LoadedDocument:
    location = config.url or config.file
    assert location is not None  # enforced by OpenApiIntrospectionConfig's validator
    text, is_yaml = fetch_text(location, base_dir, client)
    raw = parse_document(text, is_yaml=is_yaml)

    resolver = RefResolver(
        raw,
        allow_external=config.allow_external_refs,
        allowed_hosts=frozenset(config.allowed_hosts),
        fetch_external=lambda target: _fetch_external(target, base_dir, client),
    )
    resolved = resolver.resolve()

    if not is_openapi_31(str(resolved.get("openapi", ""))):
        resolved = convert_30_schema(resolved)

    base_url = resolve_base_url(resolved, base_url_override)
    return LoadedDocument(document=resolved, base_url=base_url, warnings=tuple(resolver.warnings))


def _binding(raw: RawOperation) -> HttpBinding:
    parameters = tuple(
        Parameter(
            arg=p.name,
            location=_LOCATION[p.location],
            wire_name=p.name,
            required=p.required,
            schema=p.schema,
            explode=p.explode,
        )
        for p in raw.parameters
    )
    body = (
        BodySpec(content_type=raw.request_body["content_type"], schema=raw.request_body["schema"])
        if raw.request_body is not None
        else None
    )
    return HttpBinding(method=raw.method.upper(), path=raw.path, parameters=parameters, body=body)


class OpenApiSource:
    def __init__(
        self, upstream_key: str, loaded: LoadedDocument, *, include_deprecated: bool
    ) -> None:
        self._upstream_key = upstream_key
        self._document = loaded.document
        self._include_deprecated = include_deprecated

    def operations(self) -> Iterable[Operation]:
        for raw in extract_raw_operations(
            self._document, include_deprecated=self._include_deprecated
        ):
            op = self._build(raw)
            if op is not None:
                yield op

    def _build(self, raw: RawOperation) -> Operation | None:
        if raw.extensions.get("x-mcp-exclude"):
            return None

        binding = _binding(raw)
        try:
            derived_effect = effect_for_method(binding.method)
        except UnsupportedMethod:
            return None

        try:
            check_path_template(binding)
            resolve_arg_names(binding)
            input_schema = build_input_schema(binding)
        except FlattenError as exc:
            log.warning(
                "upstream %r: dropping %s %s: %s",
                self._upstream_key,
                raw.method.upper(),
                raw.path,
                exc,
            )
            return None

        op_id = raw.operation_id or _default_id(raw)
        description = raw.extensions.get("x-mcp-description") or raw.description or raw.summary or op_id
        title = raw.extensions.get("x-mcp-title") or raw.summary or description.splitlines()[0]
        name = self._name_override(raw)

        return Operation(
            id=op_id,
            upstream=self._upstream_key,
            name=name,
            title=title,
            description=description,
            group_tags=raw.tags,
            effect=derived_effect,
            sensitivity=Sensitivity.SENSITIVE if raw.extensions.get("x-mcp-sensitive") else Sensitivity.NORMAL,
            input_schema=input_schema,
            binding=binding,
        )

    def _name_override(self, raw: RawOperation) -> str:
        name = raw.extensions.get("x-mcp-name")
        if not name:
            return ""
        if not NAME_PATTERN.fullmatch(name):
            log.warning(
                "upstream %r: x-mcp-name %r on %s %s does not match the published tool-name "
                "pattern; falling back to a generated name",
                self._upstream_key,
                name,
                raw.method.upper(),
                raw.path,
            )
            return ""
        return name


def _default_id(raw: RawOperation) -> str:
    return default_operation_id(raw.method, raw.path)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_source_openapi.py -v`
Expected: 12 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/sources/openapi.py tests/test_source_openapi.py
git commit -m "feat: build Operations from an OpenAPI document"
```

---

### Task 7: Header denylist for introspected operations

**Files:**
- Modify: `src/mcp_portal/config/loader.py`
- Modify: `tests/test_config_loader.py`

**Interfaces:**
- Consumes: `Operation`, `HttpBinding` (P1 `operations.py`).
- Produces: `credential_headers_for(config: Config) -> frozenset[str]`,
  `check_operation_headers(operations: Iterable[Operation], credential_headers: frozenset[str]) -> None`.

Introspected operations never pass through `config.operations`, so
`check_header_denylist`, which walks `config.operations`, cannot see them. This
task adds the same check as a function over the general `Operation` list —
additive, not a replacement: `check_header_denylist` still runs at config-load
time over explicit entries (unchanged, P1 behavior); `check_operation_headers`
runs again in `app.py` (Task 10) once introspected and explicit operations are
merged, which is redundant for explicit-only configs and necessary for
introspected ones.

- [ ] **Step 1: Write the failing test**

Add these imports to the top of `tests/test_config_loader.py`:

```python
from mcp_portal.config.loader import check_operation_headers, credential_headers_for
from mcp_portal.config.models import Config
from mcp_portal.operations import Effect, HttpBinding, Operation, Parameter, ParamLocation, Sensitivity
```

Then append:

```python
def header_op(wire_name: str) -> Operation:
    return Operation(
        id="x",
        upstream="billing",
        name="",
        title="x",
        description="d",
        group_tags=(),
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(
            method="GET",
            path="/x",
            parameters=(
                Parameter(
                    arg="h",
                    location=ParamLocation.HEADER,
                    wire_name=wire_name,
                    required=False,
                    schema={"type": "string"},
                ),
            ),
        ),
    )


def test_credential_headers_for_collects_every_configured_outbound_header(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("K", "v")
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {"outbound": {"mode": "static", "header": "X-Api-Key", "value": "${env:K}"}},
            }
        }
    }
    cfg = Config.model_validate(payload)
    assert credential_headers_for(cfg) == frozenset({"x-api-key"})


def test_credential_headers_for_is_empty_when_no_upstream_configures_static_auth():
    cfg = Config.model_validate(MINIMAL)
    assert credential_headers_for(cfg) == frozenset()


def test_check_operation_headers_rejects_a_denylisted_header():
    with pytest.raises(ConfigError) as exc:
        check_operation_headers([header_op("Authorization")], frozenset())
    assert "Authorization" in str(exc.value)


def test_check_operation_headers_rejects_a_configured_credential_header():
    with pytest.raises(ConfigError):
        check_operation_headers([header_op("X-Api-Key")], frozenset({"x-api-key"}))


def test_check_operation_headers_allows_an_ordinary_header():
    check_operation_headers([header_op("X-Trace-Id")], frozenset())  # does not raise
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config_loader.py -v -k "credential_headers_for or check_operation_headers"`
Expected: FAIL — `ImportError: cannot import name 'credential_headers_for'`

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/config/loader.py`, add the import and two functions. Change
the import line:

```python
from mcp_portal.operations import HttpBinding, Operation, ParamLocation
```

Add after `check_header_denylist`:

```python
def credential_headers_for(config: Config) -> frozenset[str]:
    return frozenset(
        u.auth.outbound.header.lower()
        for u in config.upstreams.values()
        if u.auth.outbound.mode != "none"
    )


def check_operation_headers(operations: Iterable[Operation], credential_headers: frozenset[str]) -> None:
    """The same denylist as `check_header_denylist`, applied to any Operation list.

    Explicit entries are checked here a second time as a side effect of the
    caller merging both sources before this runs; that redundancy is harmless.
    """
    for op in operations:
        if not isinstance(op.binding, HttpBinding):
            continue
        for param in op.binding.parameters:
            if param.location is not ParamLocation.HEADER:
                continue
            if _is_denylisted(param.wire_name, credential_headers):
                raise ConfigError(
                    f"operation {op.id!r} declares header parameter {param.wire_name!r}, "
                    "which is denylisted: a caller-supplied value here can bypass "
                    "the outbound credential or reshape the request"
                )
```

Add `from collections.abc import Iterable` to the imports at the top of the file.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config_loader.py -v`
Expected: all passed (previous tests plus 3 new).

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/config/loader.py tests/test_config_loader.py
git commit -m "feat: extend the header denylist check to introspected operations"
```

---

### Task 8: Merge explicit and introspected operations

**Files:**
- Create: `src/mcp_portal/sources/merge.py`
- Create: `tests/test_source_merge.py`

**Interfaces:**
- Consumes: `Operation` (P1 `operations.py`).
- Produces: `merge_operations(introspected: Iterable[Operation], explicit: Iterable[Operation]) -> list[Operation]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_source_merge.py`:

```python
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity
from mcp_portal.sources.merge import merge_operations


def op(op_id: str, title: str = "t") -> Operation:
    return Operation(
        id=op_id,
        upstream="billing",
        name="",
        title=title,
        description="d",
        group_tags=(),
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method="GET", path="/x"),
    )


def test_an_explicit_entry_with_a_new_id_is_appended():
    merged = merge_operations([op("a")], [op("b")])
    assert [o.id for o in merged] == ["a", "b"]


def test_an_explicit_entry_with_a_matching_id_replaces_the_introspected_one():
    merged = merge_operations([op("a", title="from-openapi")], [op("a", title="from-config")])
    assert [o.title for o in merged] == ["from-config"]


def test_introspected_order_is_preserved_and_new_explicit_entries_come_last():
    merged = merge_operations([op("a"), op("b")], [op("c"), op("a", title="patched")])
    assert [o.id for o in merged] == ["a", "b", "c"]
    assert merged[0].title == "patched"


def test_no_introspected_operations_is_just_the_explicit_list():
    merged = merge_operations([], [op("a"), op("b")])
    assert [o.id for o in merged] == ["a", "b"]


def test_no_explicit_operations_is_just_the_introspected_list():
    merged = merge_operations([op("a"), op("b")], [])
    assert [o.id for o in merged] == ["a", "b"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_source_merge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.sources.merge'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/sources/merge.py`:

```python
"""Merge introspected and explicit operations by id (§6 of the design).

An explicit entry whose `id` matches an introspected operation replaces it
entirely; an entry with a new `id` is added. This is how an operator corrects
a bad description or a wrong `effect` without abandoning introspection.
"""

from collections.abc import Iterable

from mcp_portal.operations import Operation


def merge_operations(
    introspected: Iterable[Operation], explicit: Iterable[Operation]
) -> list[Operation]:
    introspected_list = list(introspected)
    explicit_by_id = {op.id: op for op in explicit}

    merged = [explicit_by_id.get(op.id, op) for op in introspected_list]

    seen = {op.id for op in introspected_list}
    merged.extend(op for op in explicit_by_id.values() if op.id not in seen)
    return merged
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_source_merge.py -v`
Expected: 5 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/sources/merge.py tests/test_source_merge.py
git commit -m "feat: merge introspected and explicit operations by id"
```

---

### Task 9: Registry mode posture

**Files:**
- Modify: `src/mcp_portal/registry.py`
- Modify: `tests/test_registry.py`

**Interfaces:**
- Consumes: `Effect`, `Sensitivity`, `Operation` (P1 `operations.py`).
- Produces: `apply_mode_posture(operations: Sequence[Operation], mode: str) -> list[Operation]`.
  `build_toolset`'s signature is unchanged — it already receives `config`, and
  now reads `config.mode`.

Order matters and is stated in §5 of the design: selection runs "after the
mode's posture and after classification" — so the pipeline becomes
classify → posture → select → name.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_registry.py`:

```python
def test_introspect_safe_keeps_only_read_only_normal_sensitivity_operations():
    ops = [
        op("a", effect=Effect.READ_ONLY),
        op("b", effect=Effect.ACTION),
    ]
    ts = build_toolset(ops, cfg(mode="introspect-safe"))
    assert [o.id for o in ts.operations] == ["a"]


def test_introspect_safe_excludes_sensitive_read_only_operations():
    sensitive = dataclasses.replace(op("a", effect=Effect.READ_ONLY), sensitivity=Sensitivity.SENSITIVE)
    ts = build_toolset([sensitive, op("b")], cfg(mode="introspect-safe"))
    assert [o.id for o in ts.operations] == ["b"]


def test_configured_mode_applies_no_posture_filter():
    ops = [op("a", effect=Effect.ACTION)]
    ts = build_toolset(ops, cfg())
    assert [o.id for o in ts.operations] == ["a"]


def test_introspect_unsafe_applies_no_posture_filter():
    ops = [op("a", effect=Effect.ACTION)]
    ts = build_toolset(ops, cfg(mode="introspect-unsafe", acknowledge_unsafe=True))
    assert [o.id for o in ts.operations] == ["a"]


def test_posture_runs_before_selection():
    # introspect-safe drops "b" for being an action; selection would otherwise
    # have kept it. If posture ran after selection, "b" would survive.
    ops = [op("a", effect=Effect.READ_ONLY), op("b", effect=Effect.ACTION)]
    ts = build_toolset(ops, cfg(mode="introspect-safe", selection={"include_ids": ["a", "b"]}))
    assert [o.id for o in ts.operations] == ["a"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_registry.py -v -k introspect_safe`
Expected: FAIL — `ValidationError` from `cfg(mode="introspect-safe")` if the
config model doesn't accept it yet (it does after Task 1), or the posture
tests fail on assertion since `apply_mode_posture` doesn't exist yet and
`build_toolset` doesn't filter.

No new imports are needed: `Effect`, `Sensitivity`, and `dataclasses` are
already imported at the top of this file.

- [ ] **Step 3: Write the implementation**

In `src/mcp_portal/registry.py`, change the import line:

```python
from mcp_portal.operations import Effect, Operation, Sensitivity
```

Add after `_classify`:

```python
def apply_mode_posture(operations: Sequence[Operation], mode: str) -> list[Operation]:
    """`introspect-safe` keeps only read-only, non-sensitive operations (§2, §4).

    Every other mode is unfiltered here: `configured` never introspects, and
    `introspect-unsafe` is unfiltered by definition — its gate is the
    `acknowledge_unsafe` config field, checked once at load (Task 1), not a
    per-operation filter here.
    """
    if mode != "introspect-safe":
        return list(operations)
    return [
        op for op in operations if op.effect is Effect.READ_ONLY and op.sensitivity is Sensitivity.NORMAL
    ]
```

Modify `build_toolset`:

```python
def build_toolset(operations: Iterable[Operation], config: Config) -> ToolSet:
    classified = _classify(list(operations), config.classification)
    postured = apply_mode_posture(classified, config.mode)
    selected, warnings = _select(postured, config.selection)

    options = NamingOptions(
        strategy=config.naming.strategy,
        prefix_with_group_tag=config.naming.prefix_with_group_tag,
        prefix_with_upstream=config.naming.prefix_with_upstream,
    )
    named = _assign_names(selected, options)

    by_name = {op.name: op for op in named}
    assert len(by_name) == len(named), "a tool name was shadowed despite the collision check"
    return ToolSet(operations=named, by_name=by_name, warnings=tuple(warnings))
```

(Only the middle line changes — `selected, warnings = _select(classified, ...)`
becomes `postured = apply_mode_posture(...)` followed by
`selected, warnings = _select(postured, ...)`.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_registry.py -v`
Expected: all passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/registry.py tests/test_registry.py
git commit -m "feat: apply introspect-safe exposure posture between classify and select"
```

---

### Task 10: Wire introspection into `build_app`

**Files:**
- Modify: `src/mcp_portal/app.py`
- Modify: `tests/test_app.py`
- Create: `tests/fixtures/openapi/billing.json` (used by the new app tests)

**Interfaces:**
- Consumes: everything from Tasks 1–9.
- Produces: no new public interface; `build_app` now introspects, merges,
  re-checks headers, and logs the `introspect-unsafe` banner.

- [ ] **Step 1: Create the fixture document**

Create `tests/fixtures/openapi/billing.json`:

```json
{
  "openapi": "3.1.0",
  "servers": [{ "url": "https://api.example.com/v1" }],
  "paths": {
    "/invoices": {
      "get": {
        "operationId": "list_invoices",
        "summary": "List invoices.",
        "tags": ["billing"],
        "responses": {}
      }
    },
    "/invoices/{id}": {
      "delete": {
        "operationId": "cancel_invoice",
        "summary": "Cancel an invoice.",
        "parameters": [
          { "name": "id", "in": "path", "required": true, "schema": { "type": "string" } }
        ],
        "responses": {}
      }
    }
  }
}
```

- [ ] **Step 2: Write the failing test**

`tests/test_app.py` has no existing helper for writing an arbitrary config
dict to disk — its one fixture, `config_path`, always writes the module-level
`CONFIG` dict. Add a small local helper alongside it rather than reusing
`config_path`, and append these tests to `tests/test_app.py`:

```python
import logging
import shutil

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"


def _write_config(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


@pytest.mark.anyio
async def test_build_app_introspects_and_serves_openapi_operations(tmp_path: Path):
    shutil.copy(FIXTURES / "billing.json", tmp_path / "billing.json")
    config_path = _write_config(
        tmp_path,
        {
            "version": "1",
            "mode": "introspect-safe",
            "server": {"name": "s", "transport": "stdio"},
            "upstreams": {"billing": {"introspection": {"openapi": {"file": "billing.json"}}}},
        },
    )
    app = build_app(load_config(config_path))
    try:
        names = {t.name for t in app.invoker.tools()}
        assert "list_invoices" in names
        assert "cancel_invoice" not in names  # DELETE isn't read_only; introspect-safe drops it
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_build_app_merges_an_explicit_override_by_id(tmp_path: Path):
    shutil.copy(FIXTURES / "billing.json", tmp_path / "billing.json")
    config_path = _write_config(
        tmp_path,
        {
            "version": "1",
            "mode": "introspect-safe",
            "server": {"name": "s", "transport": "stdio"},
            "upstreams": {"billing": {"introspection": {"openapi": {"file": "billing.json"}}}},
            "operations": [
                {
                    "id": "list_invoices",
                    "upstream": "billing",
                    "description": "Overridden description.",
                    "binding": {"method": "GET", "path": "/invoices"},
                }
            ],
        },
    )
    app = build_app(load_config(config_path))
    try:
        tool = next(t for t in app.invoker.tools() if t.name == "list_invoices")
        assert tool.description == "Overridden description."
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_build_app_logs_a_banner_for_introspect_unsafe(tmp_path: Path, caplog):
    shutil.copy(FIXTURES / "billing.json", tmp_path / "billing.json")
    config_path = _write_config(
        tmp_path,
        {
            "version": "1",
            "mode": "introspect-unsafe",
            "acknowledge_unsafe": True,
            "server": {"name": "s", "transport": "stdio"},
            "upstreams": {"billing": {"introspection": {"openapi": {"file": "billing.json"}}}},
        },
    )
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        app = build_app(load_config(config_path))
    try:
        names = {t.name for t in app.invoker.tools()}
        assert "cancel_invoice" in names  # unsafe mode keeps the DELETE
        assert "cancel_invoice" in caplog.text
        assert "introspect-unsafe" in caplog.text
    finally:
        await app.aclose()
```

`json`, `Path`, `pytest`, `build_app`, and `load_config` are already imported
at the top of `tests/test_app.py`; only `logging` and `shutil` are new.

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_app.py -v -k introspect`
Expected: FAIL — introspected operations are not yet wired in, so
`list_invoices` is absent from `app.invoker.tools()`.

- [ ] **Step 4: Write the implementation**

Replace `src/mcp_portal/app.py` with:

```python
"""Wire a loaded config into a runnable application."""

import logging
from dataclasses import dataclass

import httpx

from mcp_portal.auth.outbound import credential_for
from mcp_portal.config.loader import (
    ConfigError,
    LoadedConfig,
    check_operation_headers,
    credential_headers_for,
)
from mcp_portal.config.models import Config
from mcp_portal.naming import NameCollisionError
from mcp_portal.operations import Effect, Operation
from mcp_portal.registry import build_toolset
from mcp_portal.server.mcp import ToolInvoker
from mcp_portal.sources.explicit import ExplicitSource
from mcp_portal.sources.merge import merge_operations
from mcp_portal.sources.openapi import OpenApiError, OpenApiSource, load_document
from mcp_portal.sources.refs import RefError
from mcp_portal.transports.http import HttpTransport

log = logging.getLogger("mcp_portal")


@dataclass(slots=True)
class App:
    invoker: ToolInvoker
    warnings: tuple[str, ...]
    _clients: list[httpx.AsyncClient]

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()


def _introspect(config: Config, base_dir) -> tuple[list[Operation], dict[str, str]]:
    """Fetch and parse every upstream's OpenAPI document, when configured.

    Returns the introspected operations and each upstream's resolved base URL —
    `base_url` when the operator set one, otherwise the document's own `servers[]`.
    """
    introspected: list[Operation] = []
    resolved_base_urls: dict[str, str] = {}
    with httpx.Client() as client:
        for key, upstream in config.upstreams.items():
            resolved_base_urls[key] = upstream.base_url or ""
            if upstream.introspection is None:
                continue
            try:
                loaded = load_document(
                    upstream.introspection.openapi, base_dir, upstream.base_url, client
                )
            except (OpenApiError, RefError) as exc:
                raise ConfigError(f"upstream {key!r}: {exc}") from exc

            for warning in loaded.warnings:
                log.warning("upstream %r: %s", key, warning)

            resolved_base_urls[key] = upstream.base_url or loaded.base_url
            source = OpenApiSource(
                key, loaded, include_deprecated=upstream.introspection.openapi.include_deprecated
            )
            introspected.extend(source.operations())
    return introspected, resolved_base_urls


def build_app(loaded: LoadedConfig) -> App:
    config = loaded.config

    introspected: list[Operation] = []
    resolved_base_urls = {key: u.base_url or "" for key, u in config.upstreams.items()}
    if config.mode != "configured":
        introspected, resolved_base_urls = _introspect(config, loaded.base_dir)

    explicit = list(ExplicitSource(config).operations())
    operations = merge_operations(introspected, explicit)

    check_operation_headers(operations, credential_headers_for(config))

    try:
        toolset = build_toolset(operations, config)
    except NameCollisionError as exc:
        raise ConfigError(str(exc)) from exc

    for warning in toolset.warnings:
        log.warning("%s", warning)

    if config.mode == "introspect-unsafe":
        risky = sorted(op.name for op in toolset.operations if op.effect is not Effect.READ_ONLY)
        log.warning(
            "mode 'introspect-unsafe': exposing %d state-changing tool(s): %s",
            len(risky),
            ", ".join(risky) or "(none)",
        )

    clients: list[httpx.AsyncClient] = []
    transports = {}
    for key, upstream in config.upstreams.items():
        client = httpx.AsyncClient()
        clients.append(client)
        transports[key] = HttpTransport(
            client=client,
            upstream=upstream.model_copy(update={"base_url": resolved_base_urls[key]}),
            credential=credential_for(upstream.auth.outbound, loaded.secrets),
        )

    log.info("serving %d tool(s) from %d upstream(s)", len(toolset.operations), len(transports))
    return App(
        invoker=ToolInvoker(toolset, transports),
        warnings=toolset.warnings,
        _clients=clients,
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_app.py -v`
Expected: all passed.

- [ ] **Step 6: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 7: Commit**

```bash
git add src/mcp_portal/app.py tests/test_app.py tests/fixtures/openapi/billing.json
git commit -m "feat: wire OpenAPI introspection into build_app, add unsafe-mode banner"
```

---

### Task 11: Golden fixture tests — dialect, circular refs, SSRF, path merging together

**Files:**
- Create: `tests/fixtures/openapi/billing-3.1.yaml`
- Create: `tests/fixtures/openapi/billing-3.0.json`
- Create: `tests/test_openapi_golden.py`

**Interfaces:**
- Consumes: `load_document`, `OpenApiSource` (Task 6).
- Produces: no new production interface — this task is entirely tests, per
  §11 of the design ("golden tests: OpenAPI 3.0 and 3.1 fixtures... snapshot
  to expected ToolSets").

- [ ] **Step 1: Create the 3.1 fixture**

Create `tests/fixtures/openapi/billing-3.1.yaml`:

```yaml
openapi: "3.1.0"
info: { title: Billing, version: "1.0" }
servers:
  - url: https://api.example.com/v1
paths:
  /invoices:
    parameters:
      - name: customerId
        in: query
        required: true
        schema: { type: string }
    get:
      operationId: list_invoices
      summary: List invoices.
      tags: [billing]
      responses: { "200": { description: OK } }
    post:
      operationId: create_invoice
      summary: Create an invoice.
      tags: [billing]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties: { amount: { type: integer } }
              required: [amount]
      responses: { "201": { description: Created } }
  /invoices/{id}:
    get:
      operationId: get_invoice
      summary: Get an invoice.
      tags: [billing]
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: string }
      responses: { "200": { description: OK } }
    delete:
      operationId: cancel_invoice
      summary: Cancel an invoice.
      tags: [billing]
      x-mcp-sensitive: true
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: string }
      responses: { "204": { description: "No Content" } }
  /invoices/{id}/audit:
    get:
      operationId: get_invoice_audit
      summary: Internal audit trail.
      tags: [billing, internal]
      x-mcp-exclude: true
      parameters:
        - name: id
          in: path
          required: true
          schema: { type: string }
      responses: { "200": { description: OK } }
  /invoices/legacy:
    get:
      operationId: list_invoices_legacy
      summary: Deprecated legacy listing.
      deprecated: true
      tags: [billing]
      responses: { "200": { description: OK } }
```

- [ ] **Step 2: Create the 3.0 fixture with a dialect quirk and a circular ref**

Create `tests/fixtures/openapi/billing-3.0.json`:

```json
{
  "openapi": "3.0.3",
  "info": { "title": "Billing", "version": "1.0" },
  "servers": [{ "url": "https://api.example.com/v1" }],
  "components": {
    "schemas": {
      "Invoice": {
        "type": "object",
        "properties": {
          "amount": { "type": "integer", "nullable": true, "minimum": 0, "exclusiveMinimum": true },
          "parent": { "$ref": "#/components/schemas/Invoice" }
        },
        "required": ["amount"]
      }
    }
  },
  "paths": {
    "/invoices": {
      "post": {
        "operationId": "create_invoice",
        "summary": "Create an invoice.",
        "tags": ["billing"],
        "requestBody": {
          "required": true,
          "content": {
            "application/json": { "schema": { "$ref": "#/components/schemas/Invoice" } }
          }
        },
        "responses": { "201": { "description": "Created" } }
      }
    }
  }
}
```

- [ ] **Step 3: Write the failing golden tests**

Create `tests/test_openapi_golden.py`:

```python
import logging
from pathlib import Path

import httpx
import pytest

from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import Config
from mcp_portal.sources.openapi import OpenApiSource, load_document
from mcp_portal.sources.refs import RefError

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"


def build(name: str, **overrides) -> dict:
    cfg = Config.model_validate(
        {
            "version": "1",
            "mode": overrides.pop("mode", "introspect-safe"),
            "acknowledge_unsafe": overrides.pop("acknowledge_unsafe", False),
            "server": {"name": "s", "transport": "stdio"},
            "upstreams": {
                "billing": {"introspection": {"openapi": {"file": name} | overrides}}
            },
        }
    )
    loaded = load_document(
        cfg.upstreams["billing"].introspection.openapi, FIXTURES, None, httpx.Client()
    )
    ops = {
        o.id: o
        for o in OpenApiSource(
            "billing", loaded, include_deprecated=cfg.upstreams["billing"].introspection.openapi.include_deprecated
        ).operations()
    }
    return ops


def test_31_fixture_surfaces_the_expected_operations_excluding_deprecated_and_x_mcp_excluded():
    ops = build("billing-3.1.yaml")
    assert set(ops) == {"list_invoices", "create_invoice", "get_invoice", "cancel_invoice"}


def test_31_fixture_includes_deprecated_when_asked():
    ops = build("billing-3.1.yaml", include_deprecated=True)
    assert "list_invoices_legacy" in ops


def test_path_level_query_parameter_is_inherited_by_both_operations_on_the_path():
    ops = build("billing-3.1.yaml")
    assert "customer_id" in ops["list_invoices"].input_schema["properties"]


def test_x_mcp_sensitive_operation_is_dropped_under_introspect_safe():
    ops = build("billing-3.1.yaml")
    assert "cancel_invoice" not in ops  # DELETE is idempotent_write, not read_only either way


def test_30_fixture_converts_nullable_and_boolean_exclusive_minimum():
    ops = build("billing-3.0.json")
    amount = ops["create_invoice"].input_schema["properties"]["amount"]
    assert amount["type"] == ["integer", "null"]
    assert amount["exclusiveMinimum"] == 0
    assert "minimum" not in amount


def test_30_fixture_circular_ref_becomes_an_open_object_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        ops = build("billing-3.0.json")
    assert ops["create_invoice"].input_schema["properties"]["parent"] == {"type": "object"}


def test_external_ref_targeting_a_disallowed_host_is_rejected():
    doc_path = FIXTURES / "ssrf-attempt.json"
    doc_path.write_text(
        '{"openapi": "3.1.0", "servers": [{"url": "https://api.example.com"}], '
        '"paths": {"/x": {"post": {"operationId": "x", "requestBody": {"content": '
        '{"application/json": {"schema": {"$ref": '
        '"https://evil.example.com/frag.yaml#/Foo"}}}}, "responses": {}}}}}'
    )
    try:
        with pytest.raises(RefError) as exc:
            build("ssrf-attempt.json")
        assert "external" in str(exc.value)
    finally:
        doc_path.unlink()
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `uv run pytest tests/test_openapi_golden.py -v`
Expected: FAIL initially on collection or on the first assertion if any of
Tasks 1–6 have a mismatch with this fixture's shape — reconcile against the
implementation from those tasks rather than changing the fixture's intent.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_openapi_golden.py -v`
Expected: 7 passed.

- [ ] **Step 6: Run every test file this plan touched, together**

```bash
uv run pytest tests/test_config_models.py tests/test_schema_drift.py \
  tests/test_openapi_document.py tests/test_refs.py tests/test_dialect.py \
  tests/test_openapi_paths.py tests/test_source_openapi.py \
  tests/test_config_loader.py tests/test_source_merge.py tests/test_registry.py \
  tests/test_app.py tests/test_openapi_golden.py -v
```
Expected: all passed. This is not the full suite — P1's other files
(`test_operations.py`, `test_flatten.py`, `test_classify.py`, `test_naming.py`,
`test_build_request.py`, `test_http_execute.py`, `test_outbound.py`,
`test_server_mcp.py`, `test_stdio.py`, `test_cli.py`, `test_source_explicit.py`,
`test_examples.py`) are untouched by this plan and are not re-run here; run
`uv run pytest -q` yourself if you want the full-suite confirmation before
merging.

- [ ] **Step 7: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 8: Commit**

```bash
git add tests/fixtures/openapi tests/test_openapi_golden.py
git commit -m "test: add OpenAPI 3.0/3.1 golden fixtures covering dialect, circular refs, SSRF"
```
