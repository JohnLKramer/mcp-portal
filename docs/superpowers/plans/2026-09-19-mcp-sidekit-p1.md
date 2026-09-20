# mcp-portal P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working stdio MCP sidecar that serves an explicitly configured HTTP API as MCP tools, using a static credential.

**Architecture:** A one-way pipeline — config → `Operation` objects → selection → naming → `ToolSet` → MCP server. Everything before the server is pure functions over frozen dataclasses with no I/O, so the subtle logic (flattening, naming collisions, request construction) is testable without a network. The MCP SDK's lowlevel `Server` is driven by `on_list_tools`/`on_call_tool` callbacks, which is what allows tools to be generated from config at runtime rather than declared with decorators.

**Tech Stack:** Python 3.14.7, uv, hatchling, Pydantic v2, httpx, `mcp` SDK 2.2.x, ruamel.yaml, pytest, ruff, mypy.

**Spec:** [`docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md`](../specs/2026-09-19-mcp-sidekit-design.md)

## Global Constraints

- **Python `>=3.14.7`.** Use modern typing throughout: `X | None`, not `Optional[X]`; `list[str]`, not `List[str]`; `type` statements where useful. No `from __future__ import annotations` needed.
- **Scope is P1 only.** Not in this plan: OpenAPI introspection, RAR/policy, inbound auth, OAuth grants beyond `static`, the HTTP server transport, and `configure`. Do not add config fields for them.
- **A phase that publishes a config field must implement it.** `schema/config-v1.schema.json` produced by P1 contains only fields P1 honors. Later phases add fields; additions are backward-compatible so `version: "1"` holds throughout.
- **`mode` is required with no default.** P1's enum contains exactly `configured`.
- **Secrets are references only:** every secret-typed field matches `^\$\{(env|file):[^}]+\}$`. A literal is a load error.
- **`id` is the only match key.** Selection and classification match on `id`, `tags`, `upstream`, `effect`. Never on `name`.
- **Naming runs after selection.** Collision suffixes depend on the surviving set.
- **HEAD and OPTIONS are never exposed** as tools.
- **Tool names** match `^[a-z0-9_]{1,64}$`. 64 is fixed, not configurable.
- **Relative paths** (`${file:}`) resolve against the directory containing the config file, not the process CWD.
- Every task ends with `uv run ruff format .`, `uv run ruff check .`, `uv run mypy src`, and the task's tests passing before the commit step.

---

### Task 1: Project setup and the operation model

**Files:**
- Modify: `pyproject.toml`
- Create: `src/mcp_portal/operations.py`
- Create: `tests/test_operations.py`
- Delete: `tests/test_main.py` (the scaffold placeholder)

**Interfaces:**
- Consumes: nothing.
- Produces: `Effect`, `Sensitivity`, `ParamLocation`, `BodyMode`, `Parameter`, `BodySpec`, `HttpBinding`, `Binding`, `Operation`. Every later task imports from here.

- [ ] **Step 1: Add dependencies and tool config to `pyproject.toml`**

Replace the `dependencies` line and append the tool sections:

```toml
dependencies = [
    "mcp>=2.2.0,<3",
    "pydantic>=2.13,<3",
    "httpx>=0.28",
    "ruamel.yaml>=0.19",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py314"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.14"
strict = true
```

- [ ] **Step 2: Sync the environment**

Run: `uv sync`
Expected: resolves and installs `mcp`, `pydantic`, `httpx`, `ruamel.yaml`.

- [ ] **Step 3: Remove the scaffold test**

Run: `rm tests/test_main.py`

This test asserts `main()` prints `mcp-portal`. Task 13 replaces `main()` with a real CLI, so the assertion is about to become false.

- [ ] **Step 4: Write the failing test**

Create `tests/test_operations.py`:

```python
import dataclasses

import pytest

from mcp_portal.operations import (
    BodyMode,
    BodySpec,
    Effect,
    HttpBinding,
    Operation,
    Parameter,
    ParamLocation,
    Sensitivity,
)


def make_binding(**overrides: object) -> HttpBinding:
    defaults: dict[str, object] = {
        "method": "GET",
        "path": "/v1/invoices/{id}",
        "parameters": (
            Parameter(
                arg="invoice_id",
                location=ParamLocation.PATH,
                wire_name="id",
                required=True,
                schema={"type": "string"},
            ),
        ),
        "body": None,
    }
    return HttpBinding(**(defaults | overrides))  # type: ignore[arg-type]


def test_effect_and_sensitivity_are_string_enums():
    assert Effect.READ_ONLY == "read_only"
    assert Effect.IDEMPOTENT_WRITE == "idempotent_write"
    assert Effect.ACTION == "action"
    assert Sensitivity.NORMAL == "normal"
    assert Sensitivity.SENSITIVE == "sensitive"


def test_binding_carries_protocol_discriminator():
    assert make_binding().protocol == "http"


def test_operation_is_frozen():
    op = Operation(
        id="list_invoices",
        upstream="billing",
        name="billing_list_invoices",
        title="List invoices",
        description="List invoices for a customer.",
        group_tags=("billing",),
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=make_binding(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        op.name = "other"  # type: ignore[misc]


def test_body_spec_defaults_to_flatten_mode():
    body = BodySpec(content_type="application/json", schema={"type": "object"})
    assert body.mode is BodyMode.FLATTEN


def test_parameter_defaults_match_openapi_query_defaults():
    param = Parameter(
        arg="customer_id",
        location=ParamLocation.QUERY,
        wire_name="customerId",
        required=True,
        schema={"type": "string"},
    )
    assert param.style == "form"
    assert param.explode is True
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `uv run pytest tests/test_operations.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.operations'`

- [ ] **Step 6: Write the implementation**

Create `src/mcp_portal/operations.py`:

```python
"""The operation domain model. Pure data: no I/O, no imports from other modules."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class Effect(StrEnum):
    """What an operation does to upstream state. Derived from the HTTP method."""

    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    ACTION = "action"


class Sensitivity(StrEnum):
    """Whether an operation's data is safe to expose. Never derived."""

    NORMAL = "normal"
    SENSITIVE = "sensitive"


class ParamLocation(StrEnum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"


class BodyMode(StrEnum):
    FLATTEN = "flatten"
    SINGLE_ARG = "single_arg"


@dataclass(frozen=True, slots=True)
class Parameter:
    """One argument's mapping from tool-schema name to wire representation.

    `arg` is what the model sees; `wire_name` is what the upstream expects. Keeping
    both is what makes schema flattening reversible.
    """

    arg: str
    location: ParamLocation
    wire_name: str
    required: bool
    schema: Mapping[str, Any]
    style: str = "form"
    explode: bool = True


@dataclass(frozen=True, slots=True)
class BodySpec:
    content_type: str
    schema: Mapping[str, Any]
    mode: BodyMode = BodyMode.FLATTEN


@dataclass(frozen=True, slots=True)
class HttpBinding:
    method: str
    path: str
    parameters: tuple[Parameter, ...] = ()
    body: BodySpec | None = None
    protocol: Literal["http"] = "http"


# A closed union, discriminated on `protocol`. GrpcBinding and GraphQlBinding join
# it in later slices; nothing outside transports/ may inspect a binding's internals.
type Binding = HttpBinding


@dataclass(frozen=True, slots=True)
class Operation:
    """One callable upstream operation, independent of how it was discovered."""

    id: str
    upstream: str
    name: str
    title: str
    description: str
    group_tags: tuple[str, ...]
    effect: Effect
    sensitivity: Sensitivity
    input_schema: Mapping[str, Any]
    binding: Binding
    meta: Mapping[str, Any] = field(default_factory=dict)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_operations.py -v`
Expected: 5 passed

- [ ] **Step 8: Format, lint, type-check**

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src
```
Expected: all clean.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock src/mcp_portal/operations.py tests/test_operations.py
git rm --cached tests/test_main.py 2>/dev/null || true
git add -A tests/
git commit -m "feat: add operation domain model and project dependencies"
```

---

### Task 2: Input schema synthesis (flattening)

Synthesizes an operation's `input_schema` from its binding. Shared by the explicit source now and the OpenAPI source in P2, which is why it is its own module rather than living inside either.

**Files:**
- Create: `src/mcp_portal/sources/__init__.py`
- Create: `src/mcp_portal/sources/flatten.py`
- Create: `tests/test_flatten.py`

**Interfaces:**
- Consumes: `HttpBinding`, `Parameter`, `BodySpec`, `BodyMode`, `ParamLocation` from Task 1.
- Produces: `build_input_schema(binding: HttpBinding) -> dict[str, Any]`, `resolve_arg_names(binding: HttpBinding) -> HttpBinding`, and `FlattenError`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_flatten.py`:

```python
import pytest

from mcp_portal.operations import BodyMode, BodySpec, HttpBinding, Parameter, ParamLocation
from mcp_portal.sources.flatten import FlattenError, build_input_schema, resolve_arg_names


def param(arg: str, location: ParamLocation, required: bool = True, wire: str | None = None):
    return Parameter(
        arg=arg,
        location=location,
        wire_name=wire or arg,
        required=required,
        schema={"type": "string"},
    )


def test_parameters_flatten_into_one_object_schema():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices/{id}",
        parameters=(
            param("invoice_id", ParamLocation.PATH, wire="id"),
            param("customer_id", ParamLocation.QUERY, required=False),
        ),
    )
    schema = build_input_schema(binding)
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"invoice_id", "customer_id"}
    assert schema["required"] == ["invoice_id"]


def test_body_properties_are_lifted_in_flatten_mode():
    binding = HttpBinding(
        method="POST",
        path="/v1/invoices",
        body=BodySpec(
            content_type="application/json",
            schema={
                "type": "object",
                "properties": {"amount": {"type": "integer"}, "memo": {"type": "string"}},
                "required": ["amount"],
            },
        ),
    )
    schema = build_input_schema(binding)
    assert set(schema["properties"]) == {"amount", "memo"}
    assert schema["required"] == ["amount"]


def test_non_object_body_becomes_a_single_argument():
    binding = HttpBinding(
        method="POST",
        path="/v1/bulk",
        body=BodySpec(content_type="application/json", schema={"type": "array"}),
    )
    schema = build_input_schema(binding)
    assert set(schema["properties"]) == {"body"}
    assert schema["required"] == ["body"]


def test_single_arg_mode_keeps_the_body_whole():
    binding = HttpBinding(
        method="POST",
        path="/v1/invoices",
        body=BodySpec(
            content_type="application/json",
            schema={"type": "object", "properties": {"amount": {"type": "integer"}}},
            mode=BodyMode.SINGLE_ARG,
        ),
    )
    assert set(build_input_schema(binding)["properties"]) == {"body"}


def test_colliding_names_are_prefixed_with_location():
    binding = HttpBinding(
        method="GET",
        path="/v1/things/{id}",
        parameters=(
            param("id", ParamLocation.PATH),
            param("id", ParamLocation.QUERY),
        ),
    )
    resolved = resolve_arg_names(binding)
    assert {p.arg for p in resolved.parameters} == {"path_id", "query_id"}
    # wire_name is untouched: flattening is presentation only
    assert {p.wire_name for p in resolved.parameters} == {"id"}


def test_recollision_after_prefixing_is_an_error_naming_both_contributors():
    binding = HttpBinding(
        method="POST",
        path="/v1/things/{id}",
        parameters=(
            param("id", ParamLocation.PATH),
            param("id", ParamLocation.QUERY),
        ),
        body=BodySpec(
            content_type="application/json",
            schema={"type": "object", "properties": {"query_id": {"type": "string"}}},
        ),
    )
    with pytest.raises(FlattenError) as exc:
        resolve_arg_names(binding)
    assert "query_id" in str(exc.value)
    assert "body" in str(exc.value)


def test_unsupported_content_type_is_an_error():
    binding = HttpBinding(
        method="POST",
        path="/v1/upload",
        body=BodySpec(content_type="multipart/form-data", schema={"type": "object"}),
    )
    with pytest.raises(FlattenError) as exc:
        build_input_schema(binding)
    assert "multipart/form-data" in str(exc.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_flatten.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.sources'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/sources/__init__.py` (empty file).

Create `src/mcp_portal/sources/flatten.py`:

```python
"""Synthesize an MCP input schema from a binding.

A flat object schema is materially easier for a model to fill in correctly than a
nested {path, query, body} shape, and correctness at the tool boundary is the point
of the product. Flattening is presentation only: `Parameter.wire_name` retains the
name the upstream expects.
"""

from typing import Any

from mcp_portal.operations import BodyMode, BodySpec, HttpBinding, Parameter

SUPPORTED_CONTENT_TYPES = frozenset({"application/json"})

BODY_ARG = "body"


class FlattenError(Exception):
    """Raised at load time when a binding cannot produce a usable tool schema."""


def _body_properties(body: BodySpec) -> dict[str, Any] | None:
    """Top-level properties to lift, or None when the body stays a single argument."""
    if body.mode is BodyMode.SINGLE_ARG:
        return None
    if body.schema.get("type") != "object":
        return None
    return dict(body.schema.get("properties", {}))


def _check_content_type(body: BodySpec | None) -> None:
    if body is not None and body.content_type not in SUPPORTED_CONTENT_TYPES:
        raise FlattenError(
            f"unsupported request body content type {body.content_type!r}; "
            f"P1 supports {sorted(SUPPORTED_CONTENT_TYPES)}"
        )


def resolve_arg_names(binding: HttpBinding) -> HttpBinding:
    """Return the binding with colliding argument names prefixed by location.

    A name that still collides after prefixing is an error rather than a second
    rename, because a twice-renamed argument is one no operator can predict.
    """
    _check_content_type(binding.body)

    counts: dict[str, int] = {}
    for p in binding.parameters:
        counts[p.arg] = counts.get(p.arg, 0) + 1

    body_props = _body_properties(binding.body) if binding.body else None
    body_names = set(body_props or ({BODY_ARG: {}} if binding.body else {}))
    for name in body_names:
        counts[name] = counts.get(name, 0) + 1

    renamed: list[Parameter] = []
    for p in binding.parameters:
        arg = f"{p.location.value}_{p.arg}" if counts[p.arg] > 1 else p.arg
        renamed.append(
            Parameter(
                arg=arg,
                location=p.location,
                wire_name=p.wire_name,
                required=p.required,
                schema=p.schema,
                style=p.style,
                explode=p.explode,
            )
        )

    seen: dict[str, str] = {}
    for p in renamed:
        if p.arg in seen:
            raise FlattenError(
                f"argument name {p.arg!r} collides after location prefixing: "
                f"contributed by {seen[p.arg]} and {p.location.value}"
            )
        seen[p.arg] = p.location.value
    for name in sorted(body_names):
        if name in seen:
            raise FlattenError(
                f"argument name {name!r} collides after location prefixing: "
                f"contributed by {seen[name]} and body"
            )
        seen[name] = "body"

    return HttpBinding(
        method=binding.method,
        path=binding.path,
        parameters=tuple(renamed),
        body=binding.body,
        protocol=binding.protocol,
    )


def build_input_schema(binding: HttpBinding) -> dict[str, Any]:
    """Build a self-contained JSON Schema 2020-12 object schema for the tool."""
    resolved = resolve_arg_names(binding)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for p in resolved.parameters:
        properties[p.arg] = dict(p.schema)
        if p.required:
            required.append(p.arg)

    if resolved.body is not None:
        body_props = _body_properties(resolved.body)
        if body_props is None:
            properties[BODY_ARG] = dict(resolved.body.schema)
            required.append(BODY_ARG)
        else:
            properties.update(body_props)
            required.extend(resolved.body.schema.get("required", []))

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_flatten.py -v`
Expected: 7 passed

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/sources tests/test_flatten.py
git commit -m "feat: synthesize flat tool input schemas from bindings"
```

---

### Task 3: Effect classification

**Files:**
- Create: `src/mcp_portal/classify.py`
- Create: `tests/test_classify.py`

**Interfaces:**
- Consumes: `Effect` from Task 1.
- Produces: `effect_for_method(method: str) -> Effect`, `UnsupportedMethod`, `EXPOSED_METHODS`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_classify.py`:

```python
import pytest

from mcp_portal.classify import UnsupportedMethod, effect_for_method
from mcp_portal.operations import Effect


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("GET", Effect.READ_ONLY),
        ("PUT", Effect.IDEMPOTENT_WRITE),
        ("DELETE", Effect.IDEMPOTENT_WRITE),
        ("POST", Effect.ACTION),
        ("PATCH", Effect.ACTION),
    ],
)
def test_effect_derives_from_http_method(method: str, expected: Effect):
    assert effect_for_method(method) is expected


def test_method_matching_is_case_insensitive():
    assert effect_for_method("get") is Effect.READ_ONLY


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "TRACE"])
def test_methods_that_are_never_exposed_raise(method: str):
    with pytest.raises(UnsupportedMethod):
        effect_for_method(method)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_classify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.classify'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/classify.py`:

```python
"""Derive an operation's effect from its HTTP method.

RFC 9110 already defines safe and idempotent method semantics, so the correct
default is available for free and needs no hand-authoring.
"""

from mcp_portal.operations import Effect

_METHOD_EFFECT: dict[str, Effect] = {
    "GET": Effect.READ_ONLY,
    "PUT": Effect.IDEMPOTENT_WRITE,
    "DELETE": Effect.IDEMPOTENT_WRITE,
    "POST": Effect.ACTION,
    "PATCH": Effect.ACTION,
}

EXPOSED_METHODS = frozenset(_METHOD_EFFECT)


class UnsupportedMethod(Exception):
    """Raised for a method that is never exposed as a tool."""


def effect_for_method(method: str) -> Effect:
    """Map an HTTP method to an Effect.

    HEAD and OPTIONS raise: they carry no response body a model can use and exist
    for cache and CORS negotiation, so they are dropped at the source in every mode.
    """
    try:
        return _METHOD_EFFECT[method.upper()]
    except KeyError:
        raise UnsupportedMethod(
            f"HTTP method {method!r} is never exposed as a tool; "
            f"exposed methods are {sorted(EXPOSED_METHODS)}"
        ) from None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_classify.py -v`
Expected: 9 passed

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/classify.py tests/test_classify.py
git commit -m "feat: derive operation effect from HTTP method"
```

---

### Task 4: Tool naming and collision resolution

**Files:**
- Create: `src/mcp_portal/naming.py`
- Create: `tests/test_naming.py`

**Interfaces:**
- Consumes: `Operation` from Task 1.
- Produces: `NamingOptions` (frozen dataclass: `strategy: str = "operation_id"`, `prefix_with_group_tag: bool = False`, `prefix_with_upstream: bool = False`), `generate_names(ops, options) -> dict[str, str]`, `NameCollisionError`, `MAX_NAME_LENGTH`, `normalize(text) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_naming.py`:

```python
import pytest

from mcp_portal.naming import (
    MAX_NAME_LENGTH,
    NameCollisionError,
    NamingOptions,
    generate_names,
    normalize,
)
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity


def op(op_id: str, upstream: str = "billing", tags: tuple[str, ...] = ("billing",)) -> Operation:
    return Operation(
        id=op_id,
        upstream=upstream,
        name="",
        title=op_id,
        description="",
        group_tags=tags,
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method="GET", path="/x"),
    )


def test_normalize_lowercases_and_replaces_separators():
    assert normalize("List-Invoices By.Customer") == "list_invoices_by_customer"


def test_operation_id_strategy_uses_the_id():
    names = generate_names([op("listInvoices")], NamingOptions())
    assert names["listInvoices"] == "listinvoices"


def test_group_tag_prefix_uses_the_first_tag_in_document_order():
    options = NamingOptions(prefix_with_group_tag=True)
    names = generate_names([op("list", tags=("billing", "admin"))], options)
    assert names["list"] == "billing_list"


def test_operation_with_no_tags_gets_no_prefix():
    options = NamingOptions(prefix_with_group_tag=True)
    names = generate_names([op("list", tags=())], options)
    assert names["list"] == "list"


def test_upstream_prefix_precedes_group_tag_prefix():
    options = NamingOptions(prefix_with_group_tag=True, prefix_with_upstream=True)
    names = generate_names([op("list")], options)
    assert names["list"] == "billing_billing_list"


def test_collisions_suffix_every_collider_deterministically():
    ops = [op("list-invoices"), op("list_invoices")]
    names = generate_names(ops, NamingOptions())
    assert names["list-invoices"] != names["list_invoices"]
    assert all(n.startswith("list_invoices_") for n in names.values())
    assert names == generate_names(ops, NamingOptions())


def test_long_names_are_truncated_and_suffixed_within_the_cap():
    names = generate_names([op("a" * 200)], NamingOptions())
    assert len(names["a" * 200]) <= MAX_NAME_LENGTH


def test_names_always_match_the_mcp_name_pattern():
    import re

    names = generate_names([op("Weird Name!! 99"), op("x" * 100)], NamingOptions())
    for name in names.values():
        assert re.fullmatch(r"[a-z0-9_]{1,64}", name), name


def test_unresolvable_collision_raises_rather_than_renaming_again():
    # Two ids whose normalized form AND hash suffix would have to coincide is
    # impossible in practice, so we force it: identical ids are a config error.
    with pytest.raises(NameCollisionError):
        generate_names([op("dup"), op("dup")], NamingOptions())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_naming.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.naming'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/naming.py`:

```python
"""Generate MCP tool names from operations.

Names are presentation only. They change with strategy, prefixes, truncation and
collision suffixes, so nothing in the system matches on them — `Operation.id` is
the match key everywhere.

Runs after selection: collision suffixes depend on the surviving set, so naming
first would let removing one operation silently rename another.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from mcp_portal.operations import HttpBinding, Operation

# Several MCP clients cap tool names at 64 characters. Upstream and tag prefixes
# plus a collision suffix consume that budget quickly. Fixed, not configurable.
MAX_NAME_LENGTH = 64
_SUFFIX_LENGTH = 6
_TRUNCATE_TO = MAX_NAME_LENGTH - _SUFFIX_LENGTH - 1

_INVALID = re.compile(r"[^a-z0-9]+")
NAME_PATTERN = re.compile(rf"[a-z0-9_]{{1,{MAX_NAME_LENGTH}}}")


class NameCollisionError(Exception):
    """Raised when a tool-name collision survives hash suffixing."""


@dataclass(frozen=True, slots=True)
class NamingOptions:
    strategy: str = "operation_id"
    prefix_with_group_tag: bool = False
    prefix_with_upstream: bool = False


def normalize(text: str) -> str:
    """Lowercase and reduce to `[a-z0-9_]`, collapsing runs of separators."""
    return _INVALID.sub("_", text.lower()).strip("_")


def _base_name(op: Operation, options: NamingOptions) -> str:
    if options.strategy == "method_path" and isinstance(op.binding, HttpBinding):
        core = f"{op.binding.method}_{op.binding.path}"
    else:
        core = op.id

    parts: list[str] = []
    if options.prefix_with_upstream:
        parts.append(op.upstream)
    if options.prefix_with_group_tag and op.group_tags:
        parts.append(op.group_tags[0])
    parts.append(core)
    return normalize("_".join(parts))


def _suffix(op_id: str) -> str:
    return hashlib.sha256(op_id.encode()).hexdigest()[:_SUFFIX_LENGTH]


def _cap(name: str) -> str:
    if len(name) <= MAX_NAME_LENGTH:
        return name
    return f"{name[:_TRUNCATE_TO]}_{_suffix(name)}"


def generate_names(
    operations: Iterable[Operation],
    options: NamingOptions,
) -> dict[str, str]:
    """Map operation id to tool name, resolving collisions deterministically."""
    ops = list(operations)

    ids = [op.id for op in ops]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise NameCollisionError(f"duplicate operation ids: {sorted(duplicates)}")

    bases = {op.id: _cap(_base_name(op, options)) for op in ops}

    counts: dict[str, int] = {}
    for base in bases.values():
        counts[base] = counts.get(base, 0) + 1

    names: dict[str, str] = {}
    for op_id in sorted(bases):
        base = bases[op_id]
        if counts[base] == 1:
            names[op_id] = base
            continue
        trimmed = base[:_TRUNCATE_TO]
        names[op_id] = f"{trimmed}_{_suffix(op_id)}"

    final = list(names.values())
    unresolved = {n for n in final if final.count(n) > 1}
    if unresolved:
        raise NameCollisionError(f"tool name collision survived suffixing: {sorted(unresolved)}")

    return names
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_naming.py -v`
Expected: 9 passed

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/naming.py tests/test_naming.py
git commit -m "feat: generate tool names with deterministic collision resolution"
```

---

### Task 5: Config models and generated JSON Schema

**Files:**
- Create: `src/mcp_portal/config/__init__.py`
- Create: `src/mcp_portal/config/models.py`
- Create: `src/mcp_portal/config/schema.py`
- Create: `schema/config-v1.schema.json`
- Create: `tests/test_config_models.py`
- Create: `tests/test_schema_drift.py`

**Interfaces:**
- Consumes: `Effect`, `Sensitivity`, `ParamLocation`, `BodyMode` from Task 1.
- Produces: `Config`, `ServerConfig`, `UpstreamConfig`, `OutboundConfig`, `SelectionConfig`, `ClassificationRule`, `NamingConfig`, `OperationEntry`, `BindingEntry`, `ParameterEntry`, `BodyEntry`, `SecretRef`, and `config_json_schema() -> dict`, `SCHEMA_PATH`.

- [ ] **Step 1: Write the failing test for the models**

Create `tests/test_config_models.py`:

```python
import pytest
from pydantic import ValidationError

from mcp_portal.config.models import Config

MINIMAL: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "billing-portal", "transport": "stdio"},
    "upstreams": {"billing": {"base_url": "https://api.example.com"}},
    "operations": [
        {
            "id": "list_invoices",
            "upstream": "billing",
            "description": "List invoices.",
            "binding": {"method": "GET", "path": "/v1/invoices"},
        }
    ],
}


def test_minimal_config_validates():
    cfg = Config.model_validate(MINIMAL)
    assert cfg.mode == "configured"
    assert cfg.upstreams["billing"].timeout_ms == 30000


def test_mode_is_required_and_has_no_default():
    payload = {k: v for k, v in MINIMAL.items() if k != "mode"}
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "mode" in str(exc.value)


def test_p1_mode_enum_contains_only_configured():
    payload = MINIMAL | {"mode": "introspect-safe"}
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_unknown_version_is_rejected():
    payload = MINIMAL | {"version": "2"}
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_unknown_top_level_field_is_rejected():
    payload = MINIMAL | {"policy": {"file": "./p.yaml"}}
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "policy" in str(exc.value)


def test_literal_secret_is_rejected():
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {"outbound": {"mode": "static", "value": "sk-live-abc123"}},
            }
        }
    }
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "value" in str(exc.value)


def test_secret_reference_is_accepted():
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {"outbound": {"mode": "static", "value": "${env:BILLING_KEY}"}},
            }
        }
    }
    cfg = Config.model_validate(payload)
    outbound = cfg.upstreams["billing"].auth.outbound
    assert outbound.value == "${env:BILLING_KEY}"
    assert outbound.header == "Authorization"
    assert outbound.scheme == "Bearer"


def test_static_mode_requires_a_value():
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {"outbound": {"mode": "static"}},
            }
        }
    }
    with pytest.raises(ValidationError):
        Config.model_validate(payload)


def test_operation_referencing_an_unknown_upstream_is_rejected():
    payload = MINIMAL | {
        "operations": [
            {
                "id": "x",
                "upstream": "nope",
                "description": "d",
                "binding": {"method": "GET", "path": "/x"},
            }
        ]
    }
    with pytest.raises(ValidationError) as exc:
        Config.model_validate(payload)
    assert "nope" in str(exc.value)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.config'`

- [ ] **Step 3: Write the models**

Create `src/mcp_portal/config/__init__.py` (empty file).

Create `src/mcp_portal/config/models.py`:

```python
"""Pydantic models for the main config. The published JSON Schema is generated
from these, so the schema cannot drift from the code that reads it.

P1 declares only fields P1 honors. Later phases add fields; additions are
backward-compatible, so `version: "1"` holds across phases.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from mcp_portal.operations import BodyMode, Effect, ParamLocation, Sensitivity

SECRET_REF_PATTERN = r"^\$\{(env|file):[^}]+\}$"

SecretRef = Annotated[str, StringConstraints(pattern=SECRET_REF_PATTERN)]
"""A secret-typed field. Only ${env:VAR} and ${file:/path} are permitted.

A literal fails validation rather than warning, so configs are safe to commit by
construction rather than by discipline.
"""


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutboundConfig(Base):
    mode: Literal["none", "static"] = "none"
    header: str = "Authorization"
    scheme: str | None = "Bearer"
    value: SecretRef | None = None

    @model_validator(mode="after")
    def _static_needs_a_value(self) -> Self:
        if self.mode == "static" and self.value is None:
            raise ValueError("outbound mode 'static' requires 'value'")
        return self


class UpstreamAuthConfig(Base):
    outbound: OutboundConfig = Field(default_factory=OutboundConfig)


class UpstreamConfig(Base):
    protocol: Literal["http"] = "http"
    base_url: str
    timeout_ms: int = Field(default=30000, gt=0)
    max_total_ms: int | None = Field(default=None, gt=0)
    max_response_bytes: int = Field(default=1024 * 1024, gt=0)
    auth: UpstreamAuthConfig = Field(default_factory=UpstreamAuthConfig)

    @model_validator(mode="after")
    def _default_total_budget(self) -> Self:
        if self.max_total_ms is None:
            object.__setattr__(self, "max_total_ms", self.timeout_ms * 3)
        return self


class HttpServerConfig(Base):
    host: str = "127.0.0.1"
    port: int = Field(default=8080, gt=0, le=65535)
    path: str = "/mcp"


class ServerConfig(Base):
    name: str
    transport: Literal["stdio"] = "stdio"
    http: HttpServerConfig | None = None


class ParameterEntry(Base):
    arg: str
    location: ParamLocation = Field(alias="in")
    wire_name: str | None = None
    required: bool = False
    schema_: dict[str, Any] = Field(default_factory=lambda: {"type": "string"}, alias="schema")
    style: str = "form"
    explode: bool = True

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class BodyEntry(Base):
    content_type: str = "application/json"
    mode: BodyMode = BodyMode.FLATTEN
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class BindingEntry(Base):
    protocol: Literal["http"] = "http"
    method: str
    path: str
    parameters: list[ParameterEntry] = Field(default_factory=list)
    body: BodyEntry | None = None


class OperationEntry(Base):
    id: str
    upstream: str
    description: str
    title: str | None = None
    name: str | None = None
    group_tags: list[str] = Field(default_factory=list)
    effect: Effect | None = None
    sensitivity: Sensitivity | None = None
    binding: BindingEntry


class MatchSpec(Base):
    ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    upstream: list[str] = Field(default_factory=list)
    effect: list[Effect] = Field(default_factory=list)


class ClassificationRule(Base):
    match: MatchSpec
    effect: Effect | None = None
    sensitivity: Sensitivity | None = None


class SelectionConfig(Base):
    include_tags: list[str] | None = None
    exclude_tags: list[str] = Field(default_factory=list)
    include_ids: list[str] | None = None
    exclude_ids: list[str] = Field(default_factory=list)


class NamingConfig(Base):
    strategy: Literal["operation_id", "method_path"] = "operation_id"
    prefix_with_group_tag: bool = False
    prefix_with_upstream: bool = False


class Config(Base):
    version: Literal["1"]
    mode: Literal["configured"]
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
```

- [ ] **Step 4: Run the model tests to verify they pass**

Run: `uv run pytest tests/test_config_models.py -v`
Expected: 9 passed

- [ ] **Step 5: Write the failing schema-drift test**

Create `tests/test_schema_drift.py`:

```python
import json

from mcp_portal.config.schema import SCHEMA_PATH, config_json_schema


def test_committed_schema_matches_the_models():
    committed = json.loads(SCHEMA_PATH.read_text())
    assert committed == config_json_schema(), (
        "schema/config-v1.schema.json is stale. "
        "Regenerate with: uv run python -m mcp_portal.config.schema"
    )


def test_schema_declares_mode_required_with_no_default():
    schema = config_json_schema()
    assert "mode" in schema["required"]
    assert "default" not in schema["properties"]["mode"]
```

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/test_schema_drift.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.config.schema'`

- [ ] **Step 7: Write the schema generator**

Create `src/mcp_portal/config/schema.py`:

```python
"""Emit the published JSON Schema from the Pydantic models.

A hand-maintained schema drifts from the code that reads it, and a drifted schema
on a public contract is worse than no schema. CI runs test_schema_drift.py.
"""

import json
from pathlib import Path
from typing import Any

from mcp_portal.config.models import Config

SCHEMA_ID = "https://schemas.mcp-portal.dev/config-v1.schema.json"
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "config-v1.schema.json"


def config_json_schema() -> dict[str, Any]:
    schema = Config.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = SCHEMA_ID
    schema["title"] = "mcp-portal configuration"
    return schema


def write_schema() -> Path:
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(json.dumps(config_json_schema(), indent=2) + "\n")
    return SCHEMA_PATH


if __name__ == "__main__":
    print(f"wrote {write_schema()}")
```

- [ ] **Step 8: Generate the schema and verify the tests pass**

```bash
uv run python -m mcp_portal.config.schema
uv run pytest tests/test_schema_drift.py -v
```
Expected: schema written; 2 passed.

If `SCHEMA_PATH` resolves incorrectly, print it and adjust the `parents[N]` index — the file lives at `src/mcp_portal/config/schema.py`, so the repo root is three levels up.

- [ ] **Step 9: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 10: Commit**

```bash
git add src/mcp_portal/config schema/config-v1.schema.json tests/test_config_models.py tests/test_schema_drift.py
git commit -m "feat: add config models with generated JSON Schema and drift check"
```

---

### Task 6: Config loading, secret resolution, and startup validation

**Files:**
- Create: `src/mcp_portal/config/loader.py`
- Create: `tests/test_config_loader.py`

**Interfaces:**
- Consumes: `Config` from Task 5.
- Produces: `load_config(path: Path) -> LoadedConfig`, `LoadedConfig` (frozen dataclass: `config: Config`, `base_dir: Path`, `secrets: dict[str, str]`), `resolve_secret(ref, base_dir) -> str`, `ConfigError`, `DENYLISTED_HEADERS`, `check_header_denylist(config) -> None`.

The header denylist lives here rather than in the transport because rejecting at **load** time puts the failure in front of the operator who wrote the config, not a model at call time.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_loader.py`:

```python
import json
from pathlib import Path

import pytest

from mcp_portal.config.loader import ConfigError, load_config, resolve_secret

MINIMAL: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "s", "transport": "stdio"},
    "upstreams": {"billing": {"base_url": "https://api.example.com"}},
    "operations": [],
}


def write(tmp_path: Path, payload: dict, name: str = "config.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def test_loads_json(tmp_path: Path):
    loaded = load_config(write(tmp_path, MINIMAL))
    assert loaded.config.server.name == "s"
    assert loaded.base_dir == tmp_path


def test_loads_yaml(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "version: '1'\n"
        "mode: configured\n"
        "server: {name: s, transport: stdio}\n"
        "upstreams: {billing: {base_url: 'https://api.example.com'}}\n"
    )
    assert load_config(path).config.server.name == "s"


def test_env_secret_is_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BILLING_KEY", "sk-test")
    assert resolve_secret("${env:BILLING_KEY}", tmp_path) == "sk-test"


def test_missing_env_secret_is_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    with pytest.raises(ConfigError) as exc:
        resolve_secret("${env:ABSENT_KEY}", tmp_path)
    assert "ABSENT_KEY" in str(exc.value)


def test_file_secret_resolves_relative_to_the_config_directory(tmp_path: Path):
    (tmp_path / "token.txt").write_text("sk-from-file\n")
    assert resolve_secret("${file:token.txt}", tmp_path) == "sk-from-file"


def test_file_secret_strips_exactly_one_trailing_newline(tmp_path: Path):
    (tmp_path / "token.txt").write_text("sk-from-file\n\n")
    assert resolve_secret("${file:token.txt}", tmp_path) == "sk-from-file\n"


def test_missing_secret_file_is_a_config_error(tmp_path: Path):
    with pytest.raises(ConfigError):
        resolve_secret("${file:nope.txt}", tmp_path)


def test_secrets_are_resolved_at_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BILLING_KEY", "sk-test")
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {"outbound": {"mode": "static", "value": "${env:BILLING_KEY}"}},
            }
        }
    }
    loaded = load_config(write(tmp_path, payload))
    assert loaded.secrets["${env:BILLING_KEY}"] == "sk-test"


@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "authorization",
        "Host",
        "Cookie",
        "Content-Length",
        "Transfer-Encoding",
        "Proxy-Authorization",
        "X-Forwarded-For",
    ],
)
def test_denylisted_header_parameter_is_a_load_error(tmp_path: Path, header: str):
    payload = MINIMAL | {
        "operations": [
            {
                "id": "x",
                "upstream": "billing",
                "description": "d",
                "binding": {
                    "method": "GET",
                    "path": "/x",
                    "parameters": [{"arg": "h", "in": "header", "wire_name": header}],
                },
            }
        ]
    }
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, payload))
    assert header.lower() in str(exc.value).lower()


def test_the_configured_static_header_is_also_denylisted(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("K", "v")
    payload = MINIMAL | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {
                    "outbound": {"mode": "static", "header": "X-Api-Key", "value": "${env:K}"}
                },
            }
        },
        "operations": [
            {
                "id": "x",
                "upstream": "billing",
                "description": "d",
                "binding": {
                    "method": "GET",
                    "path": "/x",
                    "parameters": [{"arg": "k", "in": "header", "wire_name": "X-Api-Key"}],
                },
            }
        ],
    }
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, payload))


def test_an_ordinary_header_parameter_is_allowed(tmp_path: Path):
    payload = MINIMAL | {
        "operations": [
            {
                "id": "x",
                "upstream": "billing",
                "description": "d",
                "binding": {
                    "method": "GET",
                    "path": "/x",
                    "parameters": [{"arg": "trace", "in": "header", "wire_name": "X-Trace-Id"}],
                },
            }
        ]
    }
    assert load_config(write(tmp_path, payload)).config.operations[0].id == "x"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.config.loader'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/config/loader.py`:

```python
"""Load, resolve, and validate a config file.

Every check here runs at startup. A config that would fail on some future tool
call is a config that fails to load.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from mcp_portal.config.models import SECRET_REF_PATTERN, Config
from mcp_portal.operations import ParamLocation

_SECRET_RE = re.compile(SECRET_REF_PATTERN)

# A model-supplied Authorization header would bypass the outbound broker entirely,
# turning the sidecar into an open proxy for whatever credential the model invents.
# The rest are hop-by-hop or routing headers that let a caller reshape the request.
DENYLISTED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "host",
        "cookie",
        "set-cookie",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "trailer",
        "expect",
    }
)
DENYLISTED_PREFIXES = ("proxy-", "x-forwarded-")


class ConfigError(Exception):
    """Raised for any configuration problem. Always at startup, never at call time."""


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: Config
    base_dir: Path
    secrets: dict[str, str]


def resolve_secret(ref: str, base_dir: Path) -> str:
    """Resolve a ${env:VAR} or ${file:path} reference.

    File paths resolve against the config file's directory, not the process CWD,
    so a config behaves identically regardless of where it is launched from.
    """
    if not _SECRET_RE.fullmatch(ref):
        raise ConfigError(f"not a secret reference: {ref!r}")

    kind, _, target = ref[2:-1].partition(":")

    if kind == "env":
        value = os.environ.get(target)
        if value is None:
            raise ConfigError(f"environment variable {target!r} is not set")
        return value

    path = Path(target)
    resolved = path if path.is_absolute() else base_dir / path
    try:
        text = resolved.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read secret file {resolved}: {exc}") from exc
    return text[:-1] if text.endswith("\n") else text


def _is_denylisted(header: str, extra: frozenset[str]) -> bool:
    lowered = header.lower()
    return (
        lowered in DENYLISTED_HEADERS or lowered in extra or lowered.startswith(DENYLISTED_PREFIXES)
    )


def check_header_denylist(config: Config) -> None:
    """Reject bindings that let a caller set a security-relevant header."""
    credential_headers = frozenset(
        u.auth.outbound.header.lower()
        for u in config.upstreams.values()
        if u.auth.outbound.mode != "none"
    )
    for op in config.operations:
        for param in op.binding.parameters:
            if param.location is not ParamLocation.HEADER:
                continue
            header = param.wire_name or param.arg
            if _is_denylisted(header, credential_headers):
                raise ConfigError(
                    f"operation {op.id!r} declares header parameter {header!r}, "
                    "which is denylisted: a caller-supplied value here can bypass "
                    "the outbound credential or reshape the request"
                )


def _collect_secrets(data: Any, base_dir: Path, out: dict[str, str]) -> None:
    if isinstance(data, str):
        if _SECRET_RE.fullmatch(data):
            out[data] = resolve_secret(data, base_dir)
    elif isinstance(data, dict):
        for value in data.values():
            _collect_secrets(value, base_dir, out)
    elif isinstance(data, list):
        for value in data:
            _collect_secrets(value, base_dir, out)


def _read(path: Path) -> Any:
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc

    if path.suffix in {".yaml", ".yml"}:
        return YAML(typ="safe").load(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc


def load_config(path: Path) -> LoadedConfig:
    path = path.resolve()
    base_dir = path.parent
    raw = _read(path)

    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config {path}:\n{exc}") from exc

    check_header_denylist(config)

    secrets: dict[str, str] = {}
    _collect_secrets(raw, base_dir, secrets)

    return LoadedConfig(config=config, base_dir=base_dir, secrets=secrets)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config_loader.py -v`
Expected: 18 passed

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/config/loader.py tests/test_config_loader.py
git commit -m "feat: load config with secret resolution and header denylist"
```

---

### Task 7: The explicit operation source

**Files:**
- Create: `src/mcp_portal/sources/base.py`
- Create: `src/mcp_portal/sources/explicit.py`
- Create: `tests/test_source_explicit.py`

**Interfaces:**
- Consumes: `Config`/`OperationEntry` (Task 5), `build_input_schema`/`resolve_arg_names` (Task 2), `effect_for_method` (Task 3).
- Produces: `OperationSource` protocol with `operations() -> Iterable[Operation]`; `ExplicitSource(config: Config)`.

Names are left empty here. Naming runs after selection (Task 8), so the source cannot assign them.

- [ ] **Step 1: Write the failing test**

Create `tests/test_source_explicit.py`:

```python
import pytest

from mcp_portal.config.models import Config
from mcp_portal.operations import Effect, Sensitivity
from mcp_portal.sources.explicit import ExplicitSource

BASE: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "s", "transport": "stdio"},
    "upstreams": {"billing": {"base_url": "https://api.example.com"}},
}


def build(operations: list[dict]) -> list:
    config = Config.model_validate(BASE | {"operations": operations})
    return list(ExplicitSource(config).operations())


def test_entry_becomes_an_operation_with_derived_effect():
    (op,) = build(
        [
            {
                "id": "list",
                "upstream": "billing",
                "description": "List.",
                "binding": {"method": "GET", "path": "/v1/invoices"},
            }
        ]
    )
    assert op.id == "list"
    assert op.effect is Effect.READ_ONLY
    assert op.sensitivity is Sensitivity.NORMAL
    assert op.name == ""


def test_explicit_effect_overrides_the_derived_one():
    (op,) = build(
        [
            {
                "id": "reindex",
                "upstream": "billing",
                "description": "Reindex.",
                "effect": "action",
                "binding": {"method": "PUT", "path": "/v1/reindex"},
            }
        ]
    )
    assert op.effect is Effect.ACTION


def test_title_defaults_to_the_first_line_of_the_description():
    (op,) = build(
        [
            {
                "id": "list",
                "upstream": "billing",
                "description": "List invoices.\nMore detail.",
                "binding": {"method": "GET", "path": "/v1/invoices"},
            }
        ]
    )
    assert op.title == "List invoices."


def test_input_schema_is_synthesized_from_the_binding():
    (op,) = build(
        [
            {
                "id": "get",
                "upstream": "billing",
                "description": "Get.",
                "binding": {
                    "method": "GET",
                    "path": "/v1/invoices/{id}",
                    "parameters": [
                        {"arg": "invoice_id", "in": "path", "wire_name": "id", "required": True},
                    ],
                },
            }
        ]
    )
    assert op.input_schema["properties"]["invoice_id"] == {"type": "string"}
    assert op.input_schema["required"] == ["invoice_id"]


def test_wire_name_defaults_to_the_argument_name():
    (op,) = build(
        [
            {
                "id": "get",
                "upstream": "billing",
                "description": "Get.",
                "binding": {
                    "method": "GET",
                    "path": "/v1/x",
                    "parameters": [{"arg": "cursor", "in": "query"}],
                },
            }
        ]
    )
    assert op.binding.parameters[0].wire_name == "cursor"


def test_head_operations_are_never_exposed():
    assert (
        build(
            [
                {
                    "id": "probe",
                    "upstream": "billing",
                    "description": "Probe.",
                    "binding": {"method": "HEAD", "path": "/v1/x"},
                }
            ]
        )
        == []
    )


def test_flatten_failure_surfaces_as_a_config_error():
    from mcp_portal.config.loader import ConfigError

    with pytest.raises(ConfigError):
        build(
            [
                {
                    "id": "up",
                    "upstream": "billing",
                    "description": "Upload.",
                    "binding": {
                        "method": "POST",
                        "path": "/v1/up",
                        "body": {
                            "content_type": "multipart/form-data",
                            "schema": {"type": "object"},
                        },
                    },
                }
            ]
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_source_explicit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.sources.explicit'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/sources/base.py`:

```python
"""The source protocol.

A source answers only "what does this upstream offer". It never decides exposure —
that is the registry's job, and keeping the two apart is what stops each mode from
becoming its own introspection code path.
"""

from collections.abc import Iterable
from typing import Protocol

from mcp_portal.operations import Operation


class OperationSource(Protocol):
    def operations(self) -> Iterable[Operation]: ...
```

Create `src/mcp_portal/sources/explicit.py`:

```python
"""Turn config `operations[]` entries into Operations.

An explicit entry and an introspected operation produce the identical model; an
entry simply hand-writes what OpenAPI would have supplied. `input_schema` is
always synthesized from the binding, never hand-written, so both sources present
identical tool schemas for identical bindings.
"""

from collections.abc import Iterable

from mcp_portal.classify import UnsupportedMethod, effect_for_method
from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import BindingEntry, Config, OperationEntry
from mcp_portal.operations import (
    BodySpec,
    HttpBinding,
    Operation,
    Parameter,
    Sensitivity,
)
from mcp_portal.sources.flatten import FlattenError, build_input_schema, resolve_arg_names


def _binding(entry: BindingEntry) -> HttpBinding:
    return HttpBinding(
        method=entry.method.upper(),
        path=entry.path,
        parameters=tuple(
            Parameter(
                arg=p.arg,
                location=p.location,
                wire_name=p.wire_name or p.arg,
                required=p.required,
                schema=p.schema_,
                style=p.style,
                explode=p.explode,
            )
            for p in entry.parameters
        ),
        body=(
            BodySpec(
                content_type=entry.body.content_type,
                schema=entry.body.schema_,
                mode=entry.body.mode,
            )
            if entry.body
            else None
        ),
    )


class ExplicitSource:
    def __init__(self, config: Config) -> None:
        self._config = config

    def operations(self) -> Iterable[Operation]:
        for entry in self._config.operations:
            op = self._build(entry)
            if op is not None:
                yield op

    def _build(self, entry: OperationEntry) -> Operation | None:
        binding = _binding(entry.binding)

        try:
            effect = entry.effect or effect_for_method(binding.method)
        except UnsupportedMethod:
            # HEAD and OPTIONS are dropped at the source, in every mode.
            return None

        try:
            resolved = resolve_arg_names(binding)
            input_schema = build_input_schema(binding)
        except FlattenError as exc:
            raise ConfigError(f"operation {entry.id!r}: {exc}") from exc

        return Operation(
            id=entry.id,
            upstream=entry.upstream,
            name=entry.name or "",
            title=entry.title or next(iter(entry.description.splitlines()), entry.id),
            description=entry.description,
            group_tags=tuple(entry.group_tags),
            effect=effect,
            sensitivity=entry.sensitivity or Sensitivity.NORMAL,
            input_schema=input_schema,
            binding=resolved,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_source_explicit.py -v`
Expected: 7 passed

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/sources tests/test_source_explicit.py
git commit -m "feat: build operations from explicit config entries"
```

---

### Task 8: Registry — classification overrides, selection, naming

**Files:**
- Create: `src/mcp_portal/registry.py`
- Create: `tests/test_registry.py`

**Interfaces:**
- Consumes: `Operation` (Task 1), `NamingOptions`/`generate_names` (Task 4), `ClassificationRule`/`SelectionConfig`/`NamingConfig` (Task 5).
- Produces: `ToolSet` (frozen dataclass with `operations: tuple[Operation, ...]`, `by_name: dict[str, Operation]`, `warnings: tuple[str, ...]`), `build_toolset(operations, config) -> ToolSet`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_registry.py`:

```python
from mcp_portal.config.models import Config
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity
from mcp_portal.registry import build_toolset

BASE: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "s", "transport": "stdio"},
    "upstreams": {"billing": {"base_url": "https://api.example.com"}},
}


def op(op_id: str, tags: tuple[str, ...] = ("billing",), effect: Effect = Effect.READ_ONLY):
    return Operation(
        id=op_id,
        upstream="billing",
        name="",
        title=op_id,
        description="d",
        group_tags=tags,
        effect=effect,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method="GET", path="/x"),
    )


def cfg(**overrides) -> Config:
    return Config.model_validate(BASE | overrides)


def test_names_are_assigned_from_the_surviving_set():
    ts = build_toolset([op("list_invoices")], cfg())
    assert ts.operations[0].name == "list_invoices"
    assert ts.by_name["list_invoices"].id == "list_invoices"


def test_include_tags_is_any_match():
    ops = [op("a", tags=("billing", "x")), op("b", tags=("other",))]
    ts = build_toolset(ops, cfg(selection={"include_tags": ["billing"]}))
    assert [o.id for o in ts.operations] == ["a"]


def test_exclusions_win_over_inclusions():
    ops = [op("a", tags=("billing",))]
    ts = build_toolset(
        ops, cfg(selection={"include_tags": ["billing"], "exclude_tags": ["billing"]})
    )
    assert ts.operations == ()


def test_exclude_ids_supports_globs():
    ops = [op("list_public"), op("list_internal")]
    ts = build_toolset(ops, cfg(selection={"exclude_ids": ["*_internal"]}))
    assert [o.id for o in ts.operations] == ["list_public"]


def test_classification_rule_sets_sensitivity_by_id_glob():
    ops = [op("get_user_ssn"), op("list_invoices")]
    ts = build_toolset(
        ops, cfg(classification=[{"match": {"ids": ["*_ssn"]}, "sensitivity": "sensitive"}])
    )
    by_id = {o.id: o for o in ts.operations}
    assert by_id["get_user_ssn"].sensitivity is Sensitivity.SENSITIVE
    assert by_id["list_invoices"].sensitivity is Sensitivity.NORMAL


def test_later_classification_rules_win():
    ops = [op("x", tags=("admin",))]
    ts = build_toolset(
        ops,
        cfg(
            classification=[
                {"match": {"tags": ["admin"]}, "sensitivity": "sensitive"},
                {"match": {"ids": ["x"]}, "sensitivity": "normal"},
            ]
        ),
    )
    assert ts.operations[0].sensitivity is Sensitivity.NORMAL


def test_classification_can_override_effect():
    ops = [op("reindex", effect=Effect.IDEMPOTENT_WRITE)]
    ts = build_toolset(
        ops, cfg(classification=[{"match": {"ids": ["reindex"]}, "effect": "action"}])
    )
    assert ts.operations[0].effect is Effect.ACTION


def test_classification_runs_before_selection():
    # A rule marks the op sensitive; selection excludes it by tag afterwards.
    ops = [op("a", tags=("billing",))]
    ts = build_toolset(
        ops,
        cfg(
            classification=[{"match": {"ids": ["a"]}, "sensitivity": "sensitive"}],
            selection={"exclude_tags": ["billing"]},
        ),
    )
    assert ts.operations == ()


def test_a_selection_rule_matching_nothing_warns_but_does_not_fail():
    ts = build_toolset([op("a")], cfg(selection={"exclude_ids": ["ghost_*"]}))
    assert any("ghost_*" in w for w in ts.warnings)
    assert [o.id for o in ts.operations] == ["a"]


def test_naming_options_flow_from_config():
    ts = build_toolset([op("list")], cfg(naming={"prefix_with_group_tag": True}))
    assert ts.operations[0].name == "billing_list"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.registry'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/registry.py`:

```python
"""Turn a stream of Operations into the final ToolSet.

Order is load-bearing: classify, then select, then name. Collision suffixes depend
on the surviving set, so naming before selection would let removing one operation
silently rename another.

Matching is on `id`, `tags`, `upstream`, and `effect` — never on `name`. Names are
unstable by construction, so a name-keyed rule that stops matching after a
collision suffix appears would fail open.
"""

import dataclasses
from collections.abc import Iterable, Sequence
from fnmatch import fnmatch
from typing import Any

from mcp_portal.config.models import ClassificationRule, Config, MatchSpec, SelectionConfig
from mcp_portal.naming import NamingOptions, generate_names
from mcp_portal.operations import Operation


@dataclasses.dataclass(frozen=True, slots=True)
class ToolSet:
    operations: tuple[Operation, ...]
    by_name: dict[str, Operation]
    warnings: tuple[str, ...] = ()


def _matches(op: Operation, spec: MatchSpec) -> bool:
    if spec.ids and not any(fnmatch(op.id, pattern) for pattern in spec.ids):
        return False
    if spec.tags and not set(spec.tags) & set(op.group_tags):
        return False
    if spec.upstream and op.upstream not in spec.upstream:
        return False
    if spec.effect and op.effect not in spec.effect:
        return False
    return True


def _classify(ops: Sequence[Operation], rules: Sequence[ClassificationRule]) -> list[Operation]:
    result: list[Operation] = []
    for op in ops:
        for rule in rules:  # later rules win
            if not _matches(op, rule.match):
                continue
            changes: dict[str, Any] = {}
            if rule.effect is not None:
                changes["effect"] = rule.effect
            if rule.sensitivity is not None:
                changes["sensitivity"] = rule.sensitivity
            if changes:
                op = dataclasses.replace(op, **changes)
        result.append(op)
    return result


def _select(
    ops: Sequence[Operation], selection: SelectionConfig
) -> tuple[list[Operation], list[str]]:
    warnings: list[str] = []
    used: set[str] = set()

    def keep(op: Operation) -> bool:
        if selection.include_tags is not None:
            if not set(selection.include_tags) & set(op.group_tags):
                return False
            used.update(set(selection.include_tags) & set(op.group_tags))
        if selection.include_ids is not None:
            hits = [p for p in selection.include_ids if fnmatch(op.id, p)]
            if not hits:
                return False
            used.update(hits)
        hit_tags = set(selection.exclude_tags) & set(op.group_tags)
        if hit_tags:
            used.update(hit_tags)
            return False
        hit_ids = [p for p in selection.exclude_ids if fnmatch(op.id, p)]
        if hit_ids:
            used.update(hit_ids)
            return False
        return True

    kept = [op for op in ops if keep(op)]

    declared = set(selection.exclude_tags) | set(selection.exclude_ids)
    declared |= set(selection.include_tags or [])
    declared |= set(selection.include_ids or [])
    for rule in sorted(declared - used):
        warnings.append(f"selection rule {rule!r} matched no operations; it may be stale")
    return kept, warnings


def build_toolset(operations: Iterable[Operation], config: Config) -> ToolSet:
    classified = _classify(list(operations), config.classification)
    selected, warnings = _select(classified, config.selection)

    options = NamingOptions(
        strategy=config.naming.strategy,
        prefix_with_group_tag=config.naming.prefix_with_group_tag,
        prefix_with_upstream=config.naming.prefix_with_upstream,
    )
    generated = generate_names(selected, options)

    named = tuple(dataclasses.replace(op, name=op.name or generated[op.id]) for op in selected)
    return ToolSet(
        operations=named,
        by_name={op.name: op for op in named},
        warnings=tuple(warnings),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_registry.py -v`
Expected: 10 passed

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/registry.py tests/test_registry.py
git commit -m "feat: build tool set with classification, selection, and naming"
```

---

### Task 9: `build_request()` — the security-critical pure function

**Files:**
- Create: `src/mcp_portal/transports/__init__.py`
- Create: `src/mcp_portal/transports/base.py`
- Create: `src/mcp_portal/transports/http.py`
- Create: `tests/test_build_request.py`

**Interfaces:**
- Consumes: `HttpBinding`, `Parameter`, `ParamLocation`, `BodyMode` (Task 1).
- Produces: `Credential` (frozen dataclass: `header: str`, `value: str`), `PreparedRequest` (frozen dataclass: `method`, `url`, `headers: dict[str, str]`, `body: bytes | None`), `build_request(binding, base_url, args, credential) -> PreparedRequest`, `RequestBuildError`, `TransportAdapter` protocol.

This is the highest-risk code in the system. Every test below maps to a named hazard.

- [ ] **Step 1: Write the failing test**

Create `tests/test_build_request.py`:

```python
import json

import pytest

from mcp_portal.operations import BodyMode, BodySpec, HttpBinding, Parameter, ParamLocation
from mcp_portal.transports.http import Credential, RequestBuildError, build_request

BASE = "https://api.example.com"


def p(arg: str, location: ParamLocation, wire: str | None = None, required: bool = True, **kw):
    return Parameter(
        arg=arg,
        location=location,
        wire_name=wire or arg,
        required=required,
        schema={"type": "string"},
        **kw,
    )


def test_path_parameters_are_substituted():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices/{id}",
        parameters=(p("invoice_id", ParamLocation.PATH, wire="id"),),
    )
    req = build_request(binding, BASE, {"invoice_id": "inv_1"}, None)
    assert req.url == "https://api.example.com/v1/invoices/inv_1"
    assert req.method == "GET"


@pytest.mark.parametrize(
    "value",
    ["../admin/reset", "..%2Fadmin", "a/b", "a?b", "a#b", "%2e%2e%2fadmin", "http://evil/x"],
)
def test_path_traversal_and_separators_cannot_escape_the_segment(value: str):
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices/{id}",
        parameters=(p("invoice_id", ParamLocation.PATH, wire="id"),),
    )
    req = build_request(binding, BASE, {"invoice_id": value}, None)
    tail = req.url.removeprefix("https://api.example.com/v1/invoices/")
    assert "/" not in tail
    assert "?" not in tail
    assert "#" not in tail
    assert req.url.startswith("https://api.example.com/v1/invoices/")


def test_empty_required_path_value_is_an_error():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices/{id}",
        parameters=(p("invoice_id", ParamLocation.PATH, wire="id"),),
    )
    with pytest.raises(RequestBuildError):
        build_request(binding, BASE, {"invoice_id": ""}, None)


def test_query_parameters_are_encoded_and_use_wire_names():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices",
        parameters=(p("customer_id", ParamLocation.QUERY, wire="customerId"),),
    )
    req = build_request(binding, BASE, {"customer_id": "a b&c"}, None)
    assert req.url == "https://api.example.com/v1/invoices?customerId=a+b%26c"


def test_exploded_array_query_repeats_the_key():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices",
        parameters=(p("status", ParamLocation.QUERY, explode=True),),
    )
    req = build_request(binding, BASE, {"status": ["open", "paid"]}, None)
    assert req.url.endswith("?status=open&status=paid")


def test_non_exploded_array_query_is_comma_joined():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices",
        parameters=(p("status", ParamLocation.QUERY, explode=False),),
    )
    req = build_request(binding, BASE, {"status": ["open", "paid"]}, None)
    assert req.url.endswith("?status=open%2Cpaid")


def test_omitted_optional_parameters_are_dropped():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices",
        parameters=(p("cursor", ParamLocation.QUERY, required=False),),
    )
    assert build_request(binding, BASE, {}, None).url == "https://api.example.com/v1/invoices"


def test_missing_required_parameter_is_an_error():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices",
        parameters=(p("customer_id", ParamLocation.QUERY),),
    )
    with pytest.raises(RequestBuildError):
        build_request(binding, BASE, {}, None)


def test_header_parameters_are_sent():
    binding = HttpBinding(
        method="GET",
        path="/v1/x",
        parameters=(p("trace", ParamLocation.HEADER, wire="X-Trace-Id"),),
    )
    req = build_request(binding, BASE, {"trace": "abc"}, None)
    assert req.headers["X-Trace-Id"] == "abc"


@pytest.mark.parametrize("value", ["a\r\nX-Evil: 1", "a\nX-Evil: 1", "a\rX-Evil: 1"])
def test_crlf_in_a_header_value_is_rejected(value: str):
    binding = HttpBinding(
        method="GET",
        path="/v1/x",
        parameters=(p("trace", ParamLocation.HEADER, wire="X-Trace-Id"),),
    )
    with pytest.raises(RequestBuildError):
        build_request(binding, BASE, {"trace": value}, None)


def test_flattened_body_is_reassembled_with_wire_names():
    binding = HttpBinding(
        method="POST",
        path="/v1/invoices",
        body=BodySpec(
            content_type="application/json",
            schema={"type": "object", "properties": {"amount": {"type": "integer"}}},
        ),
    )
    req = build_request(binding, BASE, {"amount": 100}, None)
    assert json.loads(req.body or b"") == {"amount": 100}
    assert req.headers["Content-Type"] == "application/json"


def test_single_arg_body_is_sent_whole():
    binding = HttpBinding(
        method="POST",
        path="/v1/bulk",
        body=BodySpec(
            content_type="application/json", schema={"type": "array"}, mode=BodyMode.SINGLE_ARG
        ),
    )
    req = build_request(binding, BASE, {"body": [1, 2]}, None)
    assert json.loads(req.body or b"") == [1, 2]


def test_credential_is_attached():
    binding = HttpBinding(method="GET", path="/v1/x")
    req = build_request(binding, BASE, {}, Credential(header="Authorization", value="Bearer t"))
    assert req.headers["Authorization"] == "Bearer t"


def test_credential_is_attached_last_and_wins():
    binding = HttpBinding(
        method="GET",
        path="/v1/x",
        parameters=(p("auth", ParamLocation.HEADER, wire="X-Api-Key", required=False),),
    )
    req = build_request(
        binding, BASE, {"auth": "model-supplied"}, Credential(header="X-Api-Key", value="real")
    )
    assert req.headers["X-Api-Key"] == "real"


def test_base_url_trailing_slash_does_not_double_the_separator():
    binding = HttpBinding(method="GET", path="/v1/x")
    assert build_request(binding, BASE + "/", {}, None).url == "https://api.example.com/v1/x"


def test_base_url_path_prefix_is_preserved():
    binding = HttpBinding(method="GET", path="/invoices")
    req = build_request(binding, "https://api.example.com/v1", {}, None)
    assert req.url == "https://api.example.com/v1/invoices"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_build_request.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.transports'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/transports/__init__.py` (empty file).

Create `src/mcp_portal/transports/base.py`:

```python
"""The transport adapter protocol.

A transport receives a ready credential and never reasons about OAuth. That seam
is why later protocol slices inherit the auth broker unchanged.
"""

from typing import Protocol

from mcp_portal.operations import Operation


class ToolCallResult(Protocol):
    status: int
    text: str
    truncated: bool


class TransportAdapter(Protocol):
    async def execute(
        self, operation: Operation, arguments: dict[str, object]
    ) -> ToolCallResult: ...
```

Create `src/mcp_portal/transports/http.py`:

```python
"""Build and execute HTTP requests for an HttpBinding.

`build_request` is pure and is the highest-risk code in the system: it is the
boundary where model-supplied values become a real request.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from mcp_portal.operations import BodyMode, HttpBinding, ParamLocation

_CRLF = ("\r", "\n")


class RequestBuildError(Exception):
    """Raised when validated arguments still cannot form a safe request."""


@dataclass(frozen=True, slots=True)
class Credential:
    header: str
    value: str


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


def _encode_path_value(value: object) -> str:
    """Percent-encode against the RFC 3986 unreserved set.

    `safe=""` is the entire defence: it encodes `/`, `?`, `#` and `%`, so a value
    like `../admin/reset` cannot escape its path segment and reach an operation
    the registry never exposed.
    """
    text = str(value)
    if text == "":
        raise RequestBuildError(
            "empty value for a path parameter would collapse the segment and change the route"
        )
    return quote(text, safe="")


def _query_pairs(arg: str, value: object, explode: bool) -> list[tuple[str, str]]:
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
        if explode:
            return [(arg, v) for v in items]
        return [(arg, ",".join(items))]
    if isinstance(value, bool):
        return [(arg, "true" if value else "false")]
    return [(arg, str(value))]


def _check_header_value(name: str, value: str) -> str:
    if any(c in value for c in _CRLF):
        raise RequestBuildError(
            f"header {name!r} value contains CR or LF, which would allow header injection"
        )
    return value


def _join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def build_request(
    binding: HttpBinding,
    base_url: str,
    arguments: Mapping[str, Any],
    credential: Credential | None,
) -> PreparedRequest:
    path = binding.path
    query: list[tuple[str, str]] = []
    headers: dict[str, str] = {}
    body_fields: dict[str, Any] = {}

    for param in binding.parameters:
        if param.arg not in arguments:
            if param.required:
                raise RequestBuildError(f"missing required argument {param.arg!r}")
            continue
        value = arguments[param.arg]

        match param.location:
            case ParamLocation.PATH:
                path = path.replace("{" + param.wire_name + "}", _encode_path_value(value))
            case ParamLocation.QUERY:
                query.extend(_query_pairs(param.wire_name, value, param.explode))
            case ParamLocation.HEADER:
                headers[param.wire_name] = _check_header_value(param.wire_name, str(value))

    body: bytes | None = None
    if binding.body is not None:
        if binding.body.mode is BodyMode.SINGLE_ARG or binding.body.schema.get("type") != "object":
            if "body" in arguments:
                body = json.dumps(arguments["body"]).encode()
        else:
            declared = set(binding.body.schema.get("properties", {}))
            body_fields = {k: v for k, v in arguments.items() if k in declared}
            body = json.dumps(body_fields).encode()
        if body is not None:
            headers["Content-Type"] = binding.body.content_type

    url = _join(base_url, path)
    if query:
        url = f"{url}?{urlencode(query)}"

    # Attached last so a model-supplied header can never displace the real credential.
    if credential is not None:
        headers[credential.header] = credential.value

    return PreparedRequest(method=binding.method, url=url, headers=headers, body=body)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_build_request.py -v`
Expected: 24 passed

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/transports tests/test_build_request.py
git commit -m "feat: build HTTP requests with strict path encoding and header safety"
```

---

### Task 10: Outbound credentials (`none` and `static`)

**Files:**
- Create: `src/mcp_portal/auth/__init__.py`
- Create: `src/mcp_portal/auth/outbound.py`
- Create: `tests/test_outbound.py`

**Interfaces:**
- Consumes: `OutboundConfig` (Task 5), `LoadedConfig` (Task 6), `Credential` (Task 9).
- Produces: `credential_for(outbound: OutboundConfig, secrets: Mapping[str, str]) -> Credential | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_outbound.py`:

```python
import pytest

from mcp_portal.auth.outbound import credential_for
from mcp_portal.config.models import OutboundConfig


def test_none_mode_yields_no_credential():
    assert credential_for(OutboundConfig(mode="none"), {}) is None


def test_static_mode_applies_the_scheme():
    cfg = OutboundConfig(mode="static", value="${env:K}")
    cred = credential_for(cfg, {"${env:K}": "sk-test"})
    assert cred is not None
    assert cred.header == "Authorization"
    assert cred.value == "Bearer sk-test"


def test_static_mode_without_a_scheme_sends_a_bare_value():
    cfg = OutboundConfig(mode="static", header="X-Api-Key", scheme=None, value="${env:K}")
    cred = credential_for(cfg, {"${env:K}": "sk-test"})
    assert cred is not None
    assert cred.header == "X-Api-Key"
    assert cred.value == "sk-test"


def test_unresolved_secret_is_an_error():
    from mcp_portal.config.loader import ConfigError

    cfg = OutboundConfig(mode="static", value="${env:MISSING}")
    with pytest.raises(ConfigError):
        credential_for(cfg, {})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_outbound.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.auth'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/auth/__init__.py` (empty file).

Create `src/mcp_portal/auth/outbound.py`:

```python
"""Produce the credential to attach to upstream requests.

This module never learns which protocol it is authorizing — it yields a Credential
and the transport attaches it. P1 implements `none` and `static`; P4 adds
client_credentials and token exchange behind the same return type.
"""

from collections.abc import Mapping

from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import OutboundConfig
from mcp_portal.transports.http import Credential


def credential_for(outbound: OutboundConfig, secrets: Mapping[str, str]) -> Credential | None:
    if outbound.mode == "none":
        return None

    ref = outbound.value
    if ref is None:
        raise ConfigError("outbound mode 'static' requires 'value'")
    try:
        secret = secrets[ref]
    except KeyError:
        raise ConfigError(f"secret reference {ref!r} was not resolved at load time") from None

    value = f"{outbound.scheme} {secret}" if outbound.scheme else secret
    return Credential(header=outbound.header, value=value)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_outbound.py -v`
Expected: 4 passed

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add src/mcp_portal/auth tests/test_outbound.py
git commit -m "feat: resolve outbound credentials for none and static modes"
```

---

### Task 11: HTTP execution with retry and response mapping

**Files:**
- Modify: `src/mcp_portal/transports/http.py`
- Create: `tests/test_http_execute.py`

**Interfaces:**
- Consumes: `PreparedRequest`, `Credential` (Task 9), `UpstreamConfig` (Task 5), `Effect` (Task 1).
- Produces: `HttpResponse` (frozen dataclass: `status: int`, `text: str`, `truncated: bool`, `original_bytes: int`), `HttpTransport(client, upstream, credential)` with `async def execute(operation, arguments) -> HttpResponse`, `RETRYABLE_STATUS`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_http_execute.py`:

```python
import httpx
import pytest

from mcp_portal.config.models import UpstreamConfig
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity
from mcp_portal.transports.http import HttpTransport


def op(effect: Effect = Effect.READ_ONLY, method: str = "GET") -> Operation:
    return Operation(
        id="x",
        upstream="billing",
        name="x",
        title="x",
        description="d",
        group_tags=(),
        effect=effect,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}},
        binding=HttpBinding(method=method, path="/v1/x"),
    )


def transport(handler, **upstream_kw) -> HttpTransport:
    upstream = UpstreamConfig(base_url="https://api.example.com", **upstream_kw)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpTransport(client=client, upstream=upstream, credential=None)


@pytest.mark.anyio
async def test_successful_response_is_returned_as_text():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    result = await transport(handler).execute(op(), {})
    assert result.status == 200
    assert "ok" in result.text
    assert result.truncated is False


@pytest.mark.anyio
async def test_response_is_capped_and_marked_truncated():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 5000)

    result = await transport(handler, max_response_bytes=100).execute(op(), {})
    assert result.truncated is True
    assert result.original_bytes == 5000
    assert len(result.text.encode()) <= 200  # cap plus the truncation marker


@pytest.mark.anyio
async def test_binary_content_is_described_not_inlined():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 500,
            headers={"content-type": "image/png"},
        )

    result = await transport(handler).execute(op(), {})
    assert "image/png" in result.text
    assert "508" in result.text
    assert "PNG" not in result.text


@pytest.mark.anyio
async def test_plain_text_is_passed_through():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hello", headers={"content-type": "text/plain"})

    assert (await transport(handler).execute(op(), {})).text == "hello"


@pytest.mark.anyio
async def test_read_only_operation_retries_a_503():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 200, text="ok")

    result = await transport(handler).execute(op(Effect.READ_ONLY), {})
    assert calls == 2
    assert result.status == 200


@pytest.mark.anyio
async def test_action_operation_is_never_retried():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="down")

    result = await transport(handler).execute(op(Effect.ACTION, "POST"), {})
    assert calls == 1
    assert result.status == 503


@pytest.mark.anyio
async def test_4xx_is_not_retried():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="nope")

    result = await transport(handler).execute(op(), {})
    assert calls == 1
    assert result.status == 404


@pytest.mark.anyio
async def test_retries_are_bounded_at_three_attempts():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="bad gateway")

    result = await transport(handler).execute(op(), {})
    assert calls == 3
    assert result.status == 502
```

Add to `tests/conftest.py` (create it):

```python
import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
```

Add `anyio` to the dev group so `pytest.mark.anyio` resolves:

```bash
uv add --dev anyio
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_http_execute.py -v`
Expected: FAIL — `ImportError: cannot import name 'HttpTransport'`

- [ ] **Step 3: Append the transport to `src/mcp_portal/transports/http.py`**

```python
import asyncio
import random

import httpx

from mcp_portal.config.models import UpstreamConfig
from mcp_portal.operations import Effect, Operation

RETRYABLE_STATUS = frozenset({502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 0.1
_RETRY_AFTER_CAP_S = 10.0

_TEXTUAL_SUFFIXES = ("+json", "+xml")
_TEXTUAL_TYPES = frozenset({"application/json", "application/xml", "application/yaml"})


def _is_textual(content_type: str) -> bool:
    """Whether a response body is safe to inline as text.

    An empty content type is treated as textual: httpx.MockTransport and many real
    APIs omit it on small JSON bodies, and inlining a short unknown body is less
    harmful than hiding a real one.
    """
    if not content_type:
        return True
    if content_type.startswith("text/"):
        return True
    return content_type in _TEXTUAL_TYPES or content_type.endswith(_TEXTUAL_SUFFIXES)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    text: str
    truncated: bool
    original_bytes: int


class HttpTransport:
    def __init__(
        self,
        client: httpx.AsyncClient,
        upstream: UpstreamConfig,
        credential: Credential | None,
    ) -> None:
        self._client = client
        self._upstream = upstream
        self._credential = credential

    def _retryable(self, operation: Operation) -> bool:
        # Retrying a POST after a timeout is how a customer gets charged twice.
        return operation.effect in (Effect.READ_ONLY, Effect.IDEMPOTENT_WRITE)

    async def _sleep_for(self, attempt: int, response: httpx.Response | None) -> None:
        if response is not None and response.status_code == 429:
            header = response.headers.get("retry-after")
            if header and header.isdigit():
                await asyncio.sleep(min(float(header), _RETRY_AFTER_CAP_S))
                return
        backoff = _BACKOFF_BASE_S * (2**attempt)
        await asyncio.sleep(random.uniform(0, backoff))  # noqa: S311 - jitter, not crypto

    def _map(self, response: httpx.Response) -> HttpResponse:
        raw = response.content
        content_type = response.headers.get("content-type", "").split(";")[0].strip()

        # Binary is described, never inlined: an error path or a stray image
        # endpoint must not be able to dump base64 into a context window.
        if not _is_textual(content_type):
            return HttpResponse(
                status=response.status_code,
                text=f"[{len(raw)} bytes of {content_type or 'unknown content type'}, not inlined]",
                truncated=False,
                original_bytes=len(raw),
            )

        cap = self._upstream.max_response_bytes
        if len(raw) <= cap:
            return HttpResponse(response.status_code, response.text, False, len(raw))
        body = raw[:cap].decode(errors="replace")
        marker = f"\n\n[truncated: {len(raw)} bytes total, {cap} shown]"
        return HttpResponse(response.status_code, body + marker, True, len(raw))

    async def execute(self, operation: Operation, arguments: dict[str, Any]) -> HttpResponse:
        binding = operation.binding
        assert isinstance(binding, HttpBinding)
        request = build_request(binding, self._upstream.base_url, arguments, self._credential)

        attempts = _MAX_ATTEMPTS if self._retryable(operation) else 1
        last: httpx.Response | None = None

        for attempt in range(attempts):
            try:
                last = await self._client.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    content=request.body,
                    timeout=self._upstream.timeout_ms / 1000,
                )
            except httpx.TimeoutException:
                if attempt == attempts - 1:
                    raise
                await self._sleep_for(attempt, None)
                continue

            retryable = last.status_code in RETRYABLE_STATUS or last.status_code == 429
            if not retryable or attempt == attempts - 1:
                return self._map(last)
            await self._sleep_for(attempt, last)

        assert last is not None
        return self._map(last)
```

Move the new imports to the top of the file alongside the existing ones; `dataclass`, `Any`, and `HttpBinding` are already imported.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_http_execute.py -v`
Expected: 8 passed

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/mcp_portal/transports/http.py tests/test_http_execute.py tests/conftest.py
git commit -m "feat: execute HTTP calls with effect-gated retry and capped responses"
```

---

### Task 12: MCP tool conversion and the invoke handler

**Files:**
- Create: `src/mcp_portal/server/__init__.py`
- Create: `src/mcp_portal/server/mcp.py`
- Create: `tests/test_server_mcp.py`

**Interfaces:**
- Consumes: `Operation`, `Effect` (Task 1), `ToolSet` (Task 8), `HttpTransport`/`HttpResponse` (Task 11).
- Produces: `to_mcp_tool(op) -> types.Tool`, `annotations_for(op) -> types.ToolAnnotations`, `ToolInvoker(toolset, transports)` with `async def call(name, arguments) -> types.CallToolResult`.

MCP SDK note: `Tool` and `ToolAnnotations` are constructed with **snake_case** field names and serialize to camelCase on the wire. `CallToolResult` takes `content=[TextContent(...)]` and `is_error`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_server_mcp.py`:

```python
import httpx
import pytest
from mcp import types

from mcp_portal.config.models import UpstreamConfig
from mcp_portal.operations import Effect, HttpBinding, Operation, Sensitivity
from mcp_portal.registry import ToolSet
from mcp_portal.server.mcp import ToolInvoker, annotations_for, to_mcp_tool
from mcp_portal.transports.http import HttpTransport


def op(effect: Effect = Effect.READ_ONLY, name: str = "list_invoices") -> Operation:
    return Operation(
        id="list_invoices",
        upstream="billing",
        name=name,
        title="List invoices",
        description="List invoices for a customer.",
        group_tags=("billing",),
        effect=effect,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        binding=HttpBinding(method="GET", path="/v1/invoices"),
    )


def test_operation_converts_to_an_mcp_tool():
    tool = to_mcp_tool(op())
    assert isinstance(tool, types.Tool)
    assert tool.name == "list_invoices"
    assert tool.title == "List invoices"
    assert tool.input_schema["properties"]["q"] == {"type": "string"}


def test_tool_serializes_with_camel_case_wire_names():
    payload = to_mcp_tool(op()).model_dump(by_alias=True, exclude_none=True)
    assert "inputSchema" in payload
    assert payload["annotations"]["readOnlyHint"] is True


@pytest.mark.parametrize(
    ("effect", "read_only", "destructive", "idempotent"),
    [
        (Effect.READ_ONLY, True, False, True),
        (Effect.IDEMPOTENT_WRITE, False, True, True),
        (Effect.ACTION, False, True, False),
    ],
)
def test_annotations_follow_the_effect(effect, read_only, destructive, idempotent):
    ann = annotations_for(op(effect))
    assert ann.read_only_hint is read_only
    assert ann.destructive_hint is destructive
    assert ann.idempotent_hint is idempotent


def test_open_world_hint_is_always_true():
    assert annotations_for(op()).open_world_hint is True


def invoker(handler, operation: Operation) -> ToolInvoker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = UpstreamConfig(base_url="https://api.example.com")
    toolset = ToolSet(operations=(operation,), by_name={operation.name: operation})
    return ToolInvoker(
        toolset=toolset,
        transports={"billing": HttpTransport(client, upstream, None)},
    )


@pytest.mark.anyio
async def test_successful_call_returns_text_content():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"invoices": []})

    result = await invoker(handler, op()).call("list_invoices", {})
    assert result.is_error is False
    assert isinstance(result.content[0], types.TextContent)
    assert "invoices" in result.content[0].text


@pytest.mark.anyio
async def test_non_2xx_sets_is_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    result = await invoker(handler, op()).call("list_invoices", {})
    assert result.is_error is True
    assert "not found" in result.content[0].text


@pytest.mark.anyio
async def test_unknown_tool_is_an_error_result_not_an_exception():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{}")

    result = await invoker(handler, op()).call("ghost", {})
    assert result.is_error is True
    assert "ghost" in result.content[0].text


@pytest.mark.anyio
async def test_invalid_arguments_produce_an_actionable_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{}")

    operation = Operation(
        id="get",
        upstream="billing",
        name="get",
        title="Get",
        description="Get.",
        group_tags=(),
        effect=Effect.READ_ONLY,
        sensitivity=Sensitivity.NORMAL,
        input_schema={"type": "object", "properties": {}, "required": ["needed"]},
        binding=HttpBinding(method="GET", path="/v1/x"),
    )
    result = await invoker(handler, operation).call("get", {})
    assert result.is_error is True
    assert "needed" in result.content[0].text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_server_mcp.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.server'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_portal/server/__init__.py` (empty file).

Create `src/mcp_portal/server/mcp.py`:

```python
"""Convert Operations to MCP tools and execute tool calls.

Error categories are chosen for what a model should *do* about them: a validation
failure names the field to fix, an upstream 4xx surfaces the upstream's own
message, and neither is retried by the client on its own initiative.
"""

from collections.abc import Mapping
from typing import Any

import jsonschema
from mcp import types

from mcp_portal.operations import Effect, Operation
from mcp_portal.registry import ToolSet
from mcp_portal.transports.http import HttpTransport

_ANNOTATIONS: dict[Effect, tuple[bool, bool, bool]] = {
    # effect: (read_only, destructive, idempotent)
    Effect.READ_ONLY: (True, False, True),
    Effect.IDEMPOTENT_WRITE: (False, True, True),
    Effect.ACTION: (False, True, False),
}


def annotations_for(operation: Operation) -> types.ToolAnnotations:
    read_only, destructive, idempotent = _ANNOTATIONS[operation.effect]
    return types.ToolAnnotations(
        title=operation.title,
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        # Always true: sidekit calls an external system whose state it does not control.
        open_world_hint=True,
    )


def to_mcp_tool(operation: Operation) -> types.Tool:
    return types.Tool(
        name=operation.name,
        title=operation.title,
        description=operation.description,
        input_schema=dict(operation.input_schema),
        annotations=annotations_for(operation),
    )


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


class ToolInvoker:
    def __init__(self, toolset: ToolSet, transports: Mapping[str, HttpTransport]) -> None:
        self._toolset = toolset
        self._transports = transports

    def tools(self) -> list[types.Tool]:
        return [to_mcp_tool(op) for op in self._toolset.operations]

    async def call(self, name: str, arguments: Mapping[str, Any] | None) -> types.CallToolResult:
        operation = self._toolset.by_name.get(name)
        if operation is None:
            return _error(f"unknown tool {name!r}")

        args = dict(arguments or {})
        try:
            jsonschema.validate(args, dict(operation.input_schema))
        except jsonschema.ValidationError as exc:
            field = ".".join(str(p) for p in exc.absolute_path)
            where = f" at {field}" if field else ""
            return _error(f"invalid arguments for {name!r}{where}: {exc.message}")

        transport = self._transports.get(operation.upstream)
        if transport is None:
            return _error(f"no transport configured for upstream {operation.upstream!r}")

        response = await transport.execute(operation, args)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=response.text)],
            is_error=not (200 <= response.status < 300),
        )
```

`jsonschema` arrives as a transitive dependency of `mcp`. Declare it directly so the dependency is intentional:

```bash
uv add "jsonschema>=4.26"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_server_mcp.py -v`
Expected: 10 passed

- [ ] **Step 5: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/mcp_portal/server tests/test_server_mcp.py
git commit -m "feat: expose operations as MCP tools with effect-derived annotations"
```

---

### Task 13: Wiring, stdio server, and CLI

**Files:**
- Create: `src/mcp_portal/app.py`
- Create: `src/mcp_portal/server/stdio.py`
- Modify: `src/mcp_portal/__init__.py`
- Create: `src/mcp_portal/cli.py`
- Modify: `pyproject.toml` (console script)
- Create: `tests/test_app.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `build_app(loaded: LoadedConfig) -> App`, `App` (holds `invoker`, `warnings`, `aclose()`), `run_stdio(app, server_name)`, `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing test for wiring**

Create `tests/test_app.py`:

```python
import json
from pathlib import Path

import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

CONFIG: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "billing-portal", "transport": "stdio"},
    "upstreams": {
        "billing": {
            "base_url": "https://api.example.com",
            "auth": {"outbound": {"mode": "static", "value": "${env:BILLING_KEY}"}},
        }
    },
    "naming": {"prefix_with_group_tag": True},
    "operations": [
        {
            "id": "list_invoices",
            "upstream": "billing",
            "description": "List invoices.",
            "group_tags": ["billing"],
            "binding": {
                "method": "GET",
                "path": "/v1/invoices",
                "parameters": [{"arg": "customer_id", "in": "query", "required": True}],
            },
        },
        {
            "id": "probe",
            "upstream": "billing",
            "description": "Probe.",
            "binding": {"method": "HEAD", "path": "/v1/ping"},
        },
    ],
}


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BILLING_KEY", "sk-test")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG))
    return path


@pytest.mark.anyio
async def test_app_exposes_configured_tools(config_path: Path):
    app = build_app(load_config(config_path))
    try:
        names = [t.name for t in app.invoker.tools()]
        assert names == ["billing_list_invoices"]
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_head_operations_are_dropped_end_to_end(config_path: Path):
    app = build_app(load_config(config_path))
    try:
        assert all("probe" not in t.name for t in app.invoker.tools())
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_tool_schema_reaches_the_client(config_path: Path):
    app = build_app(load_config(config_path))
    try:
        (tool,) = app.invoker.tools()
        assert tool.input_schema["required"] == ["customer_id"]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
    finally:
        await app.aclose()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.app'`

- [ ] **Step 3: Write the wiring**

Create `src/mcp_portal/app.py`:

```python
"""Wire a loaded config into a runnable application."""

import logging
from dataclasses import dataclass

import httpx

from mcp_portal.auth.outbound import credential_for
from mcp_portal.config.loader import LoadedConfig
from mcp_portal.registry import build_toolset
from mcp_portal.server.mcp import ToolInvoker
from mcp_portal.sources.explicit import ExplicitSource
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


def build_app(loaded: LoadedConfig) -> App:
    config = loaded.config

    operations = list(ExplicitSource(config).operations())
    toolset = build_toolset(operations, config)

    for warning in toolset.warnings:
        log.warning("%s", warning)

    clients: list[httpx.AsyncClient] = []
    transports = {}
    for key, upstream in config.upstreams.items():
        client = httpx.AsyncClient()
        clients.append(client)
        transports[key] = HttpTransport(
            client=client,
            upstream=upstream,
            credential=credential_for(upstream.auth.outbound, loaded.secrets),
        )

    log.info("serving %d tool(s) from %d upstream(s)", len(toolset.operations), len(transports))
    return App(
        invoker=ToolInvoker(toolset, transports),
        warnings=toolset.warnings,
        _clients=clients,
    )
```

- [ ] **Step 4: Run the wiring tests**

Run: `uv run pytest tests/test_app.py -v`
Expected: 3 passed

- [ ] **Step 5: Write the stdio runner**

Create `src/mcp_portal/server/stdio.py`:

```python
"""Serve the tool set over stdio.

The lowlevel Server takes on_list_tools / on_call_tool callbacks rather than
decorators, which is what lets tools come from config at runtime.
"""

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from mcp_portal.app import App


async def run_stdio(app: App, server_name: str) -> None:
    async def on_list_tools(
        context: object, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=app.invoker.tools())

    async def on_call_tool(
        context: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await app.invoker.call(params.name, params.arguments)

    server: Server[None] = Server(
        name=server_name,
        version="0.1.0",
        on_list_tools=on_list_tools,  # type: ignore[arg-type]
        on_call_tool=on_call_tool,  # type: ignore[arg-type]
    )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
```

- [ ] **Step 6: Write the failing CLI test**

Create `tests/test_cli.py`:

```python
import json
from pathlib import Path

import pytest

from mcp_portal.cli import main

CONFIG: dict = {
    "version": "1",
    "mode": "configured",
    "server": {"name": "s", "transport": "stdio"},
    "upstreams": {"billing": {"base_url": "https://api.example.com"}},
    "operations": [
        {
            "id": "list_invoices",
            "upstream": "billing",
            "description": "List invoices.",
            "binding": {"method": "GET", "path": "/v1/invoices"},
        }
    ],
}


def write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    return path


def test_validate_reports_success_and_lists_tools(tmp_path: Path, capsys):
    code = main(["validate", "--config", str(write(tmp_path, CONFIG))])
    out = capsys.readouterr().out
    assert code == 0
    assert "list_invoices" in out


def test_validate_reports_a_config_error_without_a_traceback(tmp_path: Path, capsys):
    broken = {k: v for k, v in CONFIG.items() if k != "mode"}
    code = main(["validate", "--config", str(write(tmp_path, broken))])
    assert code == 2
    assert "mode" in capsys.readouterr().err


def test_missing_config_file_exits_nonzero(tmp_path: Path, capsys):
    code = main(["validate", "--config", str(tmp_path / "absent.json")])
    assert code == 2


def test_serve_requires_a_config(capsys):
    with pytest.raises(SystemExit):
        main(["serve"])
```

- [ ] **Step 7: Run it to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_portal.cli'`

- [ ] **Step 8: Write the CLI**

Create `src/mcp_portal/cli.py`:

```python
"""Command-line entrypoint.

There is deliberately no default --mode: mode is required in config and cannot be
reached by omission.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from mcp_portal.app import build_app
from mcp_portal.config.loader import ConfigError, load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-portal")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("serve", "Serve the configured tools over stdio."),
        ("validate", "Load and validate the config, then report the tool surface."),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", required=True, type=Path)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    args = _parser().parse_args(argv)

    try:
        loaded = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        app = build_app(loaded)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "validate":
        for tool in app.invoker.tools():
            print(f"{tool.name}\t{tool.title}")
        asyncio.run(app.aclose())
        return 0

    from mcp_portal.server.stdio import run_stdio

    async def _serve() -> None:
        try:
            await run_stdio(app, loaded.config.server.name)
        finally:
            await app.aclose()

    asyncio.run(_serve())
    return 0
```

Replace `src/mcp_portal/__init__.py` with:

```python
from mcp_portal.cli import main

__all__ = ["main"]
```

Add the console script to `pyproject.toml`:

```toml
[project.scripts]
mcp-portal = "mcp_portal.cli:main"
```

- [ ] **Step 9: Run the CLI tests**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 4 passed

- [ ] **Step 10: Run the whole suite**

Run: `uv run pytest -v`
Expected: all tests pass. Note the total count for the next step.

- [ ] **Step 11: Verify the CLI end to end**

```bash
cat > /tmp/sidekit-demo.json <<'JSON'
{
  "version": "1",
  "mode": "configured",
  "server": {"name": "demo", "transport": "stdio"},
  "upstreams": {"httpbin": {"base_url": "https://httpbin.org"}},
  "operations": [
    {"id": "get_uuid", "upstream": "httpbin", "description": "Get a random UUID.",
     "binding": {"method": "GET", "path": "/uuid"}}
  ]
}
JSON
uv run mcp-portal validate --config /tmp/sidekit-demo.json
```
Expected: prints `get_uuid	Get a random UUID.` and exits 0.

- [ ] **Step 12: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 13: Commit**

```bash
git add pyproject.toml uv.lock src/mcp_portal tests/
git commit -m "feat: serve configured tools over stdio with a validate command"
```

---

### Task 14: README and P1 documentation

**Files:**
- Modify: `README.md`
- Create: `examples/billing.yaml`
- Create: `tests/test_examples.py`

**Interfaces:**
- Consumes: `load_config`, `build_app`.
- Produces: nothing importable. The example config is test-verified so documentation cannot drift from the schema.

- [ ] **Step 1: Write the failing test**

Create `tests/test_examples.py`:

```python
from pathlib import Path

import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

EXAMPLES = sorted((Path(__file__).resolve().parents[1] / "examples").glob("*.yaml"))


def test_examples_directory_is_not_empty():
    assert EXAMPLES


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
@pytest.mark.anyio
async def test_every_example_config_loads_and_builds(path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BILLING_API_KEY", "sk-test")
    app = build_app(load_config(path))
    try:
        assert app.invoker.tools()
    finally:
        await app.aclose()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_examples.py -v`
Expected: FAIL — `assert EXAMPLES` fails, the examples directory does not exist.

- [ ] **Step 3: Write the example config**

Create `examples/billing.yaml`:

```yaml
# A minimal mcp-portal configuration.
# Validate with: uv run mcp-portal validate --config examples/billing.yaml

version: "1"
mode: configured

server:
  name: billing-portal
  transport: stdio

upstreams:
  billing:
    base_url: https://api.example.com
    timeout_ms: 30000
    auth:
      outbound:
        mode: static
        header: Authorization
        scheme: Bearer
        # Secrets are references only. A literal here fails to load.
        value: ${env:BILLING_API_KEY}

naming:
  strategy: operation_id
  prefix_with_group_tag: true

# Marks an operation sensitive so introspect-safe will not expose it in P2.
classification:
  - match: { ids: ["get_customer_tax_id"] }
    sensitivity: sensitive

operations:
  - id: list_invoices
    upstream: billing
    description: List invoices for a customer.
    group_tags: [billing]
    binding:
      method: GET
      path: /v1/invoices
      parameters:
        - { arg: customer_id, in: query, wire_name: customerId, required: true,
            schema: { type: string } }
        - { arg: limit, in: query, required: false, schema: { type: integer } }

  - id: get_customer_tax_id
    upstream: billing
    description: Get a customer's tax identifier.
    group_tags: [billing]
    binding:
      method: GET
      path: /v1/customers/{id}/tax-id
      parameters:
        - { arg: customer_id, in: path, wire_name: id, required: true,
            schema: { type: string } }

  - id: create_invoice
    upstream: billing
    description: Create an invoice.
    group_tags: [billing]
    binding:
      method: POST
      path: /v1/invoices
      body:
        content_type: application/json
        mode: flatten
        schema:
          type: object
          properties:
            customer_id: { type: string }
            amount_cents: { type: integer }
          required: [customer_id, amount_cents]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_examples.py -v`
Expected: 2 passed — the directory check plus one parametrized case for `billing.yaml`.

- [ ] **Step 5: Write the README**

Replace `README.md`:

````markdown
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
````

- [ ] **Step 6: Verify the documented commands actually work**

The example references `${env:BILLING_API_KEY}`, and secrets resolve at load, so
this command fails without it set — which is the intended behavior, not a bug.
Confirm both paths:

```bash
# Unset: should exit 2 with a clear message naming the variable.
unset BILLING_API_KEY
uv run mcp-portal validate --config examples/billing.yaml; echo "exit=$?"

# Set: should list 3 tools and exit 0.
BILLING_API_KEY=sk-test uv run mcp-portal validate --config examples/billing.yaml

uv run python -m mcp_portal.config.schema
git diff --exit-code schema/config-v1.schema.json
uv run pytest -q
```
Expected: exit=2 with `BILLING_API_KEY` named; then 3 tools listed; schema
regenerates with no diff; suite passes.

- [ ] **Step 7: Format, lint, type-check**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy src
```

- [ ] **Step 8: Commit**

```bash
git add README.md examples/ tests/test_examples.py
git commit -m "docs: document P1 usage with a test-verified example config"
```

---

## Verification

After Task 14, confirm the P1 deliverable end to end:

- [ ] `uv run pytest -q` — full suite passes
- [ ] `uv run ruff format --check . && uv run ruff check .` — clean
- [ ] `uv run mypy src` — clean under `strict = true`
- [ ] `BILLING_API_KEY=sk-test uv run mcp-portal validate --config examples/billing.yaml` — lists 3 tools, exits 0
- [ ] `uv run mcp-portal validate --config examples/billing.yaml` with `BILLING_API_KEY` unset — exits 2, names the variable
- [ ] `git diff --exit-code schema/config-v1.schema.json` after regenerating — no drift
- [ ] `git status` — clean tree, everything committed

**Not run by this plan:** the full suite is the only suite, so there is nothing
deferred. If that changes, report it rather than assuming coverage.

---

## Spec coverage

Which P1 spec section each task implements, and what is deliberately absent.

| Spec section | Task |
|---|---|
| §4 operation model, two-axis risk | 1 |
| §4 tool naming, 64-char cap, collisions | 4 |
| §4 classification overrides | 8 |
| §5 config contract, generated schema, drift check | 5 |
| §5 secrets as references, relative paths | 6 |
| §5 explicit entries, arg→wire mapping | 7 |
| §5 selection (any-match tags, id globs, exclusions win) | 8 |
| §6 parameter flattening, re-collision is an error | 2 |
| §6 HEAD/OPTIONS never exposed | 3, 7 |
| §9 request construction, encoding, header denylist | 6, 9 |
| §9 response mapping, size cap, binary not inlined | 11 |
| §10 retry gated on effect, bounded attempts | 11 |
| §10 startup validation (P1 subset) | 5, 6 |
| §11 security regression tests | 6, 9 |
| §12 P1 phase boundary | 13 |

**Deliberately not implemented** (later phases, per Global Constraints): OpenAPI
introspection and dialect conversion (§6), `configure` (§7), inbound auth,
`client_credentials`, `token_exchange`, token cache, RAR coverage (§8), the
streamable HTTP transport (§8), and policy (§5). No config field for any of them
appears in P1's schema.

**Two startup-validation items from §10 are P2+ by construction** and have no P1
task: the `introspect-unsafe` acknowledgment (no such mode in P1's enum) and the
`token_exchange`/stdio conflict (no such outbound mode in P1). They belong to the
phase that introduces the field.

---

## Execution

**Plan complete and saved to `docs/superpowers/plans/2026-09-19-mcp-sidekit-p1.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
