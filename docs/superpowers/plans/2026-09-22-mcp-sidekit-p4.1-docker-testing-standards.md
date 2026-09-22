# mcp-portal P4.1 (Docker/Mocks Testing Standard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every phase through P4a a Docker-driven integration test that
proves its headline capability — including real backend-side auth
enforcement — and put the shared tooling (`Dockerfile.base` via Buildx Bake,
`mocks/*` auth) in place so every phase from here forward does the same.

**Architecture:** `mocks/billing` and `mocks/orders` (Flask, already
Docker-composed) each gain the minimum real behavior their phase needs:
`mocks/orders` checks a hardcoded API key (proves P1's `static` outbound
mode); `mocks/billing` gains an `/openapi.json` route (proves P2's
`introspect-safe` mode) and a bearer-token check against
`navikt/mock-oauth2-server`, a new prebuilt-image Compose service (proves P3's
RAR policy and P4a's `client_credentials` mode together — the policy grants
exactly the `authorization_details` the token then carries). A new root
`Dockerfile.base` supplies the layers every image shares (Python, `uv`, the
non-root user); Buildx Bake's additional-build-context mechanism
(`contexts = { pybuilder = "target:base-builder", ... }`) is how every other
Dockerfile in the repo builds `FROM` it without copying its content.

**Tech Stack:** Same as P1–P4a (Python 3.14.7, uv, Flask + gunicorn for
mocks) plus Docker Buildx Bake (`docker-bake.hcl`) and
`ghcr.io/navikt/mock-oauth2-server` (prebuilt image, not built by us).
`mocks/billing` gains `pyjwt[crypto]` and `httpx` as its own runtime
dependencies (it is a separate uv project from the main package).

**Spec:** [`docs/superpowers/specs/2026-09-22-mcp-sidekit-docker-testing-standards-design.md`](../specs/2026-09-22-mcp-sidekit-docker-testing-standards-design.md)

## Global Constraints

- **Prove the headline flow, not everything.** Each Docker test proves one
  phase's main capability. Comprehensive coverage stays a unit-test concern.
- **Mock auth is real.** A mock that's supposed to require a credential
  actually rejects requests without one — a passing test must prove the
  *backend* enforced it, not just that mcp-portal attached something.
- **`docker buildx bake` builds every image; `docker compose up` only runs
  already-built images.** Once this plan lands, `docker-compose.yml`'s
  `billing-mock`/`orders-mock`/`mcp-portal` services reference `image:` tags
  Bake produces (`billing-mock:local`, etc.), not `build:` blocks — Compose
  has no way to resolve a Dockerfile's `FROM pybuilder`/`FROM pyruntime`
  bake-context references on its own.
- **The mcp-portal process under test runs on the host** (as it already
  does in `tests/integration/test_stack.py` — `build_app` is called
  in-process, not inside a container), so every upstream URL an mcp-portal
  config points at uses a **published `localhost:<port>`** address.
  Anything a *mock container* itself needs to reach (e.g. `mocks/billing`
  fetching `navikt/mock-oauth2-server`'s JWKS) uses the **Compose service
  name** (`mock-oauth2-server`) instead, since the mock runs inside the
  Compose network, not on the host.
- **`mocks/*` are separate uv projects** with their own `pyproject.toml`/
  `uv.lock`/tests, outside the root `uv` workspace and outside root
  `ruff`/`mypy` config (`pyproject.toml`'s `[tool.ruff] exclude` already
  lists `mocks`) — don't add root-level lint/type-check steps for them; each
  mock's own `uv run pytest` is its whole verification loop.
- Every task ends with its own test command passing before the commit step.
  Docker-dependent tasks additionally require `docker buildx bake` (or the
  relevant single target) to succeed. Do not run the *entire*
  `uv run pytest -m integration` suite mid-task — the final task is where
  everything runs together.

---

### Task 1: `Dockerfile.base` + Buildx Bake, existing Dockerfiles converted

**Files:**
- Create: `Dockerfile.base`
- Create: `docker-bake.hcl`
- Modify: `Dockerfile` (main app)
- Modify: `mocks/billing/Dockerfile`
- Modify: `mocks/orders/Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `tests/integration/test_stack.py`

**Interfaces:**
- Produces: three locally-tagged images (`mcp-portal:local`,
  `billing-mock:local`, `orders-mock:local`) buildable via
  `docker buildx bake`; `docker-compose.yml` services reference these tags
  via `image:` instead of `build:`.

- [ ] **Step 1: Create the shared base image**

Create `Dockerfile.base`:

```dockerfile
# syntax=docker/dockerfile:1
# Shared layers for every mcp-portal image (the main app and every
# mocks/* service). Never built or run on its own — consumed through
# docker-bake.hcl's base-builder/base-runtime targets and the
# additional-build-context mechanism in each service's own Dockerfile.

FROM python:3.14.7-slim AS base-builder
WORKDIR /app
RUN pip install --no-cache-dir uv

FROM python:3.14.7-slim AS base-runtime
RUN groupadd -r appuser && useradd -r -g appuser appuser
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
```

- [ ] **Step 2: Write the Bake file**

Create `docker-bake.hcl`:

```hcl
group "default" {
  targets = ["mcp-portal", "billing-mock", "orders-mock"]
}

target "base-builder" {
  dockerfile = "Dockerfile.base"
  target     = "base-builder"
}

target "base-runtime" {
  dockerfile = "Dockerfile.base"
  target     = "base-runtime"
}

target "mcp-portal" {
  context    = "."
  dockerfile = "Dockerfile"
  tags       = ["mcp-portal:local"]
  contexts = {
    pybuilder = "target:base-builder"
    pyruntime = "target:base-runtime"
  }
}

target "billing-mock" {
  context    = "./mocks/billing"
  dockerfile = "Dockerfile"
  tags       = ["billing-mock:local"]
  contexts = {
    pybuilder = "target:base-builder"
    pyruntime = "target:base-runtime"
  }
}

target "orders-mock" {
  context    = "./mocks/orders"
  dockerfile = "Dockerfile"
  tags       = ["orders-mock:local"]
  contexts = {
    pybuilder = "target:base-builder"
    pyruntime = "target:base-runtime"
  }
}
```

- [ ] **Step 3: Convert the three existing Dockerfiles**

Replace `Dockerfile` (main app):

```dockerfile
FROM pybuilder AS builder
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM pyruntime AS runner
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
USER appuser
ENTRYPOINT ["mcp-portal", "serve"]
CMD ["--config", "/config/config.yaml"]
```

Replace `mocks/billing/Dockerfile`:

```dockerfile
FROM pybuilder AS builder
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM pyruntime AS runner
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
USER appuser
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--worker-tmp-dir", "/dev/shm", "billing_mock.app:app"]
```

Replace `mocks/orders/Dockerfile` identically except the final `CMD`:

```dockerfile
FROM pybuilder AS builder
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM pyruntime AS runner
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
USER appuser
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--worker-tmp-dir", "/dev/shm", "orders_mock.app:app"]
```

- [ ] **Step 4: Build with Bake and verify**

Run: `docker buildx bake`
Expected: three images built, tagged `mcp-portal:local`, `billing-mock:local`,
`orders-mock:local` (`docker images | grep local` to confirm). If Buildx
reports it doesn't recognize `contexts`, confirm the installed Buildx
version supports additional build contexts (`docker buildx version`; this
needs a reasonably current Buildx, bundled with current Docker
Desktop/Engine) and note the version in your report if you had to work
around anything.

- [ ] **Step 5: Point Compose at the built images**

Replace `docker-compose.yml`:

```yaml
services:
  billing-mock:
    image: billing-mock:local
    ports:
      - "8081:8080"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"]
      interval: 5s
      timeout: 3s
      retries: 5

  orders-mock:
    image: orders-mock:local
    ports:
      - "8082:8080"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"]
      interval: 5s
      timeout: 3s
      retries: 5
```

- [ ] **Step 6: Update the integration fixture to bake before composing up**

In `tests/integration/test_stack.py`, replace the `mock_stack` fixture's
build step (currently `docker compose up -d --build billing-mock
orders-mock`):

```python
@pytest.fixture(scope="session")
def mock_stack() -> Iterator[None]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")

    subprocess.run(
        ["docker", "buildx", "bake", "billing-mock", "orders-mock"],
        cwd=REPO_ROOT,
        check=True,
    )
    subprocess.run(
        ["docker", "compose", "up", "-d", "billing-mock", "orders-mock"],
        cwd=REPO_ROOT,
        check=True,
    )
    try:
        _wait_for_health()
        yield
    finally:
        subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=True)
```

- [ ] **Step 7: Run the existing integration suite to verify nothing broke**

Run: `uv run pytest tests/integration/test_stack.py -m integration -v`
Expected: all passing, unchanged from before this task (this task only
changes *how* images are built, not any behavior).

- [ ] **Step 8: Commit**

```bash
git add Dockerfile.base docker-bake.hcl Dockerfile mocks/billing/Dockerfile \
        mocks/orders/Dockerfile docker-compose.yml tests/integration/test_stack.py
git commit -m "build: shared Dockerfile.base via Buildx Bake for every image"
```

---

### Task 2: `mocks/orders` requires an API key (proves `static` outbound mode)

**Files:**
- Modify: `mocks/orders/src/orders_mock/app.py`
- Modify: `mocks/orders/tests/test_app.py`
- Modify: `tests/integration/fixtures/stack.yaml`
- Modify: `tests/integration/test_stack.py`

**Interfaces:**
- Produces: every `mocks/orders` route except `/healthz` requires
  `X-Api-Key: orders-mock-test-key`, else `401`.

- [ ] **Step 1: Write the failing unit tests**

Append to `mocks/orders/tests/test_app.py`:

```python
def test_list_orders_without_an_api_key_is_401(client):
    response = client.get("/v1/orders")
    assert response.status_code == 401


def test_list_orders_with_the_wrong_api_key_is_401(client):
    response = client.get("/v1/orders", headers={"X-Api-Key": "wrong"})
    assert response.status_code == 401


def test_list_orders_with_the_right_api_key_succeeds(client):
    response = client.get("/v1/orders", headers={"X-Api-Key": "orders-mock-test-key"})
    assert response.status_code == 200


def test_healthz_needs_no_api_key(client):
    response = client.get("/healthz")
    assert response.status_code == 200
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (from `mocks/orders/`): `uv run pytest -v`
Expected: the three `401`-expecting tests currently get `200` (no check
exists yet).

- [ ] **Step 3: Add the API-key check**

In `mocks/orders/src/orders_mock/app.py`, add after `app = Flask(__name__)`:

```python
_API_KEY = "orders-mock-test-key"


@app.before_request
def _require_api_key():
    if request.path == "/healthz":
        return None
    if request.headers.get("X-Api-Key") != _API_KEY:
        return jsonify(error="missing or invalid API key"), 401
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run (from `mocks/orders/`): `uv run pytest -v`
Expected: all passing.

- [ ] **Step 5: Wire the credential into the Docker integration fixture**

In `tests/integration/fixtures/stack.yaml`, change the `orders` upstream:

```yaml
  orders:
    base_url: http://localhost:8082
    timeout_ms: 5000
    auth:
      outbound:
        mode: static
        header: X-Api-Key
        scheme: null
        value: ${env:ORDERS_MOCK_API_KEY}
```

In `tests/integration/test_stack.py`, every test that calls an `orders_*`
tool needs `ORDERS_MOCK_API_KEY` set before `build_app`/`load_config` runs.
Add a fixture near the top (after the existing `MOCK_HEALTH_URLS` constant):

```python
@pytest.fixture(autouse=True)
def _orders_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDERS_MOCK_API_KEY", "orders-mock-test-key")
```

- [ ] **Step 6: Run the Docker integration suite**

Run: `uv run pytest tests/integration/test_stack.py -m integration -v`
Expected: all passing — `test_list_orders_round_trips_through_the_orders_mock`
and its siblings now genuinely exercise `static` mode; if the credential
weren't wired correctly, these would now fail with `401`-shaped errors
instead of silently passing (the mock previously accepted anything).

- [ ] **Step 7: Commit**

```bash
git add mocks/orders/src/orders_mock/app.py mocks/orders/tests/test_app.py \
        tests/integration/fixtures/stack.yaml tests/integration/test_stack.py
git commit -m "test: orders mock requires an API key, proving static outbound mode over Docker"
```

---

### Task 3: `mocks/billing` serves an OpenAPI document (proves live introspection)

**Files:**
- Modify: `mocks/billing/src/billing_mock/app.py`
- Modify: `mocks/billing/tests/test_app.py`
- Create: `tests/integration/fixtures/stack-introspection.yaml`
- Create: `tests/integration/test_stack_introspection.py`

**Interfaces:**
- Produces: `GET /openapi.json` on `mocks/billing`, describing its existing
  `GET /v1/invoices` and `POST /v1/invoices` routes with `operationId`s
  `listInvoices` and `createInvoice`.

- [ ] **Step 1: Write the failing unit test**

Append to `mocks/billing/tests/test_app.py`:

```python
def test_openapi_document_describes_list_and_create_invoices(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    doc = response.get_json()
    assert doc["paths"]["/v1/invoices"]["get"]["operationId"] == "listInvoices"
    assert doc["paths"]["/v1/invoices"]["post"]["operationId"] == "createInvoice"
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `mocks/billing/`): `uv run pytest -v -k openapi_document`
Expected: FAIL — `404` (`/openapi.json` doesn't exist).

- [ ] **Step 3: Serve the document**

In `mocks/billing/src/billing_mock/app.py`, add:

```python
_OPENAPI_DOC = {
    "openapi": "3.0.3",
    "info": {"title": "Billing Mock API", "version": "1.0.0"},
    "servers": [{"url": "http://localhost:8080"}],
    "paths": {
        "/v1/invoices": {
            "get": {
                "operationId": "listInvoices",
                "parameters": [
                    {
                        "name": "customerId",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {"200": {"description": "OK"}},
            },
            "post": {
                "operationId": "createInvoice",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "customer_id": {"type": "string"},
                                    "amount_cents": {"type": "integer"},
                                },
                                "required": ["customer_id", "amount_cents"],
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
    },
}


@app.get("/openapi.json")
def openapi_document():
    return jsonify(_OPENAPI_DOC)
```

- [ ] **Step 4: Run the test to verify it passes**

Run (from `mocks/billing/`): `uv run pytest -v`
Expected: all passing.

- [ ] **Step 5: Write the introspection stack fixture**

Create `tests/integration/fixtures/stack-introspection.yaml`:

```yaml
version: "1"
mode: introspect-safe

server:
  name: integration-portal-introspection
  transport: stdio

upstreams:
  billing:
    base_url: http://localhost:8081
    timeout_ms: 5000
    introspection:
      openapi:
        url: http://localhost:8081/openapi.json

naming:
  strategy: operation_id
```

`naming.strategy: operation_id` with no prefixing turns `operationId:
listInvoices`/`createInvoice` into tool names `listinvoices`/`createinvoice`
(`naming.normalize` lowercases and strips non-`[a-z0-9]` characters — no
underscore is inserted for a camelCase boundary).

- [ ] **Step 6: Write the Docker integration test**

Create `tests/integration/test_stack_introspection.py`:

```python
"""Docker integration test: introspect-safe mode against a live OpenAPI
document served by the billing mock. Proves P2's live-introspection flow —
`tests/test_openapi_golden.py` already proves the parser against static
fixtures; this proves the whole path against a real HTTP GET.
"""

import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
STACK_CONFIG = Path(__file__).resolve().parent / "fixtures" / "stack-introspection.yaml"
MOCK_HEALTH_URL = "http://localhost:8081/healthz"


def _wait_for_health(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(MOCK_HEALTH_URL, timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError("billing mock did not become healthy in time")


@pytest.fixture(scope="session")
def mock_stack() -> Iterator[None]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")

    subprocess.run(["docker", "buildx", "bake", "billing-mock"], cwd=REPO_ROOT, check=True)
    subprocess.run(["docker", "compose", "up", "-d", "billing-mock"], cwd=REPO_ROOT, check=True)
    try:
        _wait_for_health()
        yield
    finally:
        subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=True)


@pytest.mark.anyio
async def test_list_invoices_via_live_openapi_introspection(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        names = [t.name for t in app.invoker.tools()]
        assert "listinvoices" in names
        assert "createinvoice" in names

        result = await app.invoker.call("listinvoices", {"customerId": "cust_1"})
        assert result.is_error is False
        assert "inv_1" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_create_invoice_via_live_openapi_introspection(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        result = await app.invoker.call(
            "createinvoice", {"customer_id": "cust_5", "amount_cents": 750}
        )
        assert result.is_error is False
        assert "cust_5" in result.content[0].text
    finally:
        await app.aclose()
```

`customerId` (not `customer_id`) is the argument name here because
`introspect-safe` flattens straight from the OpenAPI parameter name — there
is no config-supplied `wire_name` remap the way `stack.yaml`'s explicit
`operations[]` entries have one.

- [ ] **Step 7: Run the Docker integration test**

Run: `uv run pytest tests/integration/test_stack_introspection.py -m integration -v`
Expected: both tests passing.

- [ ] **Step 8: Commit**

```bash
git add mocks/billing/src/billing_mock/app.py mocks/billing/tests/test_app.py \
        tests/integration/fixtures/stack-introspection.yaml \
        tests/integration/test_stack_introspection.py
git commit -m "test: billing mock serves OpenAPI, proving introspect-safe mode over Docker"
```

---

### Task 4: RAR policy allow/deny over Docker (proves P3)

**Files:**
- Create: `tests/integration/fixtures/policy.yaml`
- Create: `tests/integration/fixtures/stack-policy-denied.yaml`
- Create: `tests/integration/fixtures/stack-policy-authorized.yaml`
- Create: `tests/integration/test_stack_policy.py`

**Interfaces:**
- Consumes: `mocks/billing`'s existing (unauthenticated, pre-Task-5)
  `GET /v1/customers/<id>/tax-id` and `GET /v1/invoices` routes.
- Produces: a policy rule requiring `authorization_details: [{type:
  customer_data}]` on `get_customer_tax_id`, proven both denied (empty
  local principal) and allowed (matching local principal) against the same
  running mock.

- [ ] **Step 1: Write the policy file**

Create `tests/integration/fixtures/policy.yaml`:

```yaml
version: "1"
defaults:
  unmatched: allow
rules:
  - match:
      ids: [get_customer_tax_id]
    require:
      authorization_details:
        - type: customer_data
```

- [ ] **Step 2: Write the denied-principal stack config**

Create `tests/integration/fixtures/stack-policy-denied.yaml`:

```yaml
version: "1"
mode: configured

server:
  name: integration-portal-policy
  transport: stdio

upstreams:
  billing:
    base_url: http://localhost:8081
    timeout_ms: 5000

policy:
  file: ./policy.yaml

auth:
  local_principal:
    authorization_details: []

naming:
  strategy: operation_id
  prefix_with_group_tag: true

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
```

- [ ] **Step 3: Write the authorized-principal stack config**

Create `tests/integration/fixtures/stack-policy-authorized.yaml` — identical
to the file above, except:

```yaml
auth:
  local_principal:
    authorization_details:
      - type: customer_data
```

- [ ] **Step 4: Write the Docker integration test**

Create `tests/integration/test_stack_policy.py`:

```python
"""Docker integration test: RAR policy allow/deny against a live backend.
Proves P3's enforcement — `tests/test_p3_end_to_end.py` already proves the
same shape purely in-process; this proves it reaches a real HTTP call (or
is stopped before one, for the denied case).
"""

import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MOCK_HEALTH_URL = "http://localhost:8081/healthz"


def _wait_for_health(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(MOCK_HEALTH_URL, timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError("billing mock did not become healthy in time")


@pytest.fixture(scope="session")
def mock_stack() -> Iterator[None]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")

    subprocess.run(["docker", "buildx", "bake", "billing-mock"], cwd=REPO_ROOT, check=True)
    subprocess.run(["docker", "compose", "up", "-d", "billing-mock"], cwd=REPO_ROOT, check=True)
    try:
        _wait_for_health()
        yield
    finally:
        subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=True)


@pytest.mark.anyio
async def test_the_policy_gated_operation_is_denied_without_the_required_detail(
    mock_stack: None,
):
    app = build_app(load_config(FIXTURES / "stack-policy-denied.yaml"))
    try:
        result = await app.invoker.call(
            "billing_get_customer_tax_id", {"customer_id": "cust_1"}
        )
        assert result.is_error is True
        assert "customer_data" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_an_unmatched_operation_is_allowed_under_the_same_policy(mock_stack: None):
    app = build_app(load_config(FIXTURES / "stack-policy-denied.yaml"))
    try:
        result = await app.invoker.call(
            "billing_list_invoices", {"customer_id": "cust_1"}
        )
        assert result.is_error is False
        assert "inv_1" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_the_policy_gated_operation_is_allowed_with_the_required_detail(
    mock_stack: None,
):
    app = build_app(load_config(FIXTURES / "stack-policy-authorized.yaml"))
    try:
        result = await app.invoker.call(
            "billing_get_customer_tax_id", {"customer_id": "cust_1"}
        )
        assert result.is_error is False
        assert "TAX-CUST-1" in result.content[0].text
    finally:
        await app.aclose()
```

- [ ] **Step 5: Run the Docker integration test**

Run: `uv run pytest tests/integration/test_stack_policy.py -m integration -v`
Expected: all three passing.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/fixtures/policy.yaml \
        tests/integration/fixtures/stack-policy-denied.yaml \
        tests/integration/fixtures/stack-policy-authorized.yaml \
        tests/integration/test_stack_policy.py
git commit -m "test: RAR policy allow/deny against a live backend over Docker"
```

---

### Task 5: OAuth via `navikt/mock-oauth2-server` (proves `client_credentials`)

**Files:**
- Modify: `docker-compose.yml`
- Modify: `mocks/billing/pyproject.toml`
- Modify: `mocks/billing/src/billing_mock/app.py`
- Modify: `mocks/billing/tests/test_app.py`
- Create: `tests/integration/fixtures/stack-oauth.yaml`
- Create: `tests/integration/test_stack_oauth.py`

**Interfaces:**
- Produces: every `mocks/billing` route except `/healthz` and
  `/openapi.json` requires a bearer JWT verified against
  `navikt/mock-oauth2-server`'s JWKS, `401` otherwise.

- [ ] **Step 1: Add the mock IdP service**

In `docker-compose.yml`, add a new service (pin the version at
implementation time to whatever `navikt/mock-oauth2-server`'s current
stable release tag is — `2.1.10` below is this plan's best-effort pin,
confirm it against the project's current docs/releases before relying on
its exact `JSON_CONFIG` shape):

```yaml
  mock-oauth2-server:
    image: ghcr.io/navikt/mock-oauth2-server:2.1.10
    ports:
      - "8083:8080"
    environment:
      JSON_CONFIG: |
        {
          "interactiveLogin": false,
          "httpServer": "NettyWrapper",
          "tokenCallbacks": [
            {
              "issuerId": "default",
              "tokenExpiry": 3600,
              "requestMappings": [
                {
                  "requestParam": "grant_type",
                  "match": "client_credentials",
                  "claims": {
                    "sub": "billing-service",
                    "aud": ["billing-mock"]
                  }
                }
              ]
            }
          ]
        }
```

Add `MOCK_OAUTH2_ISSUER` to `billing-mock`'s service so the mock (running
*inside* the Compose network) reaches the IdP by service name, not by the
host-published port mcp-portal uses:

```yaml
  billing-mock:
    image: billing-mock:local
    ports:
      - "8081:8080"
    environment:
      MOCK_OAUTH2_ISSUER: http://mock-oauth2-server:8080/default
    depends_on:
      - mock-oauth2-server
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"]
      interval: 5s
      timeout: 3s
      retries: 5
```

- [ ] **Step 2: Add billing-mock's own dependencies**

In `mocks/billing/pyproject.toml`, add to `dependencies`:

```toml
    "pyjwt[crypto]>=2.14,<3",
    "httpx>=0.28",
```

Run (from `mocks/billing/`): `uv sync`

- [ ] **Step 3: Write the failing unit tests**

Append to `mocks/billing/tests/test_app.py`:

```python
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _bearer(rsa_key, **claim_overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "http://mock-oauth2-server:8080/default",
        "aud": "billing-mock",
        "exp": now + 300,
        "sub": "billing-service",
    } | claim_overrides
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-kid"})


def test_list_invoices_without_a_bearer_token_is_401(client):
    response = client.get("/v1/invoices", query_string={"customerId": "cust_1"})
    assert response.status_code == 401


def test_list_invoices_with_a_valid_bearer_token_succeeds(client, monkeypatch, rsa_key):
    from billing_mock import app as app_module

    class _FakeSigningKey:
        key = rsa_key.public_key()

    class _FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    monkeypatch.setattr(app_module, "_jwks_client", _FakeJwksClient())

    token = _bearer(rsa_key)
    response = client.get(
        "/v1/invoices",
        query_string={"customerId": "cust_1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


def test_openapi_document_needs_no_bearer_token(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
```

- [ ] **Step 4: Run the tests to verify they fail**

Run (from `mocks/billing/`): `uv run pytest -v -k "bearer_token"`
Expected: FAIL — no auth check exists yet (`_jwks_client` doesn't exist,
first test gets `200` instead of `401`).

- [ ] **Step 5: Add the bearer-token check**

In `mocks/billing/src/billing_mock/app.py`, add near the top (after
`app = Flask(__name__)`):

```python
import os

import jwt

_OAUTH_ISSUER = os.environ.get("MOCK_OAUTH2_ISSUER", "http://mock-oauth2-server:8080/default")
_jwks_client = jwt.PyJWKClient(f"{_OAUTH_ISSUER}/jwks")


@app.before_request
def _require_bearer_token():
    if request.path in ("/healthz", "/openapi.json"):
        return None
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify(error="missing bearer token"), 401
    token = auth_header.removeprefix("Bearer ")
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience="billing-mock",
            issuer=_OAUTH_ISSUER,
        )
    except jwt.InvalidTokenError:
        return jsonify(error="invalid bearer token"), 401
    return None
```

- [ ] **Step 6: Run the tests to verify they pass**

Run (from `mocks/billing/`): `uv run pytest -v`
Expected: all passing.

- [ ] **Step 7: Write the OAuth stack fixture**

Create `tests/integration/fixtures/stack-oauth.yaml`:

```yaml
version: "1"
mode: configured

server:
  name: integration-portal-oauth
  transport: stdio

upstreams:
  billing:
    base_url: http://localhost:8081
    timeout_ms: 5000
    auth:
      outbound:
        mode: client_credentials
        token_endpoint: http://localhost:8083/default/token
        client_id: billing-client
        client_secret: ${env:MOCK_OAUTH2_CLIENT_SECRET}

naming:
  strategy: operation_id
  prefix_with_group_tag: true

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
```

`mock-oauth2-server` accepts any non-empty `client_secret` for a
`client_credentials` request against its default issuer (it does not
validate registered clients unless configured to) — set
`MOCK_OAUTH2_CLIENT_SECRET` to any non-empty string in the test; confirm
this against the pinned version's current docs during implementation, and
adjust the fixture if that version does enforce client registration.

- [ ] **Step 8: Write the Docker integration test**

Create `tests/integration/test_stack_oauth.py`:

```python
"""Docker integration test: outbound.mode: client_credentials against a real
mock-oauth2-server IdP, with the billing mock genuinely verifying the
issued bearer token. Proves P4a's dynamic outbound mode over Docker.
"""

import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
STACK_CONFIG = Path(__file__).resolve().parent / "fixtures" / "stack-oauth.yaml"
MOCK_HEALTH_URLS = (
    "http://localhost:8081/healthz",
    "http://localhost:8083/default/.well-known/openid-configuration",
)


def _wait_for_health(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if all(httpx.get(url, timeout=1.0).status_code == 200 for url in MOCK_HEALTH_URLS):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError("billing mock / mock-oauth2-server did not become healthy in time")


@pytest.fixture(scope="session")
def mock_stack() -> Iterator[None]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")

    subprocess.run(
        ["docker", "buildx", "bake", "billing-mock"], cwd=REPO_ROOT, check=True
    )
    subprocess.run(
        ["docker", "compose", "up", "-d", "billing-mock", "mock-oauth2-server"],
        cwd=REPO_ROOT,
        check=True,
    )
    try:
        _wait_for_health()
        yield
    finally:
        subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=True)


@pytest.fixture(autouse=True)
def _client_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_OAUTH2_CLIENT_SECRET", "any-non-empty-value")


@pytest.mark.anyio
async def test_list_invoices_via_a_real_client_credentials_round_trip(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        result = await app.invoker.call(
            "billing_list_invoices", {"customer_id": "cust_1"}
        )
        assert result.is_error is False
        assert "inv_1" in result.content[0].text
    finally:
        await app.aclose()
```

- [ ] **Step 9: Run the Docker integration test**

Run: `uv run pytest tests/integration/test_stack_oauth.py -m integration -v`
Expected: passing. If it fails on a `401` from billing-mock, first confirm
the token endpoint / audience / issuer values in `stack-oauth.yaml` and the
mock IdP's `JSON_CONFIG` agree; this is the step most likely to need
iteration against the pinned image version's actual behavior.

- [ ] **Step 10: Commit**

```bash
git add docker-compose.yml mocks/billing/pyproject.toml mocks/billing/uv.lock \
        mocks/billing/src/billing_mock/app.py mocks/billing/tests/test_app.py \
        tests/integration/fixtures/stack-oauth.yaml tests/integration/test_stack_oauth.py
git commit -m "test: client_credentials against a real mock-oauth2-server IdP over Docker"
```

---

### Task 6: Documentation — AGENTS.md, future-work.md, phasing amendments

**Files:**
- Modify: `AGENTS.md`
- Create: `docs/superpowers/specs/future-work.md`
- Modify: `docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md`
- Modify: `docs/superpowers/plans/2026-09-22-mcp-sidekit-p4-inbound.md`

**Interfaces:**
- Produces: no code — the standing standard document, the phasing-table
  amendment, and a blocking note carried into the not-yet-started
  P4-inbound plan.

- [ ] **Step 1: Update `AGENTS.md`**

Add a new `## Technology Choices` section (place it after `## Testing`,
before `## Agent Guardrails`):

```markdown
## Technology Choices

| Concern | Choice |
|---|---|
| OpenAPI parsing | hand-rolled (`sources/openapi*.py`) + `jsonschema` |
| RAR (RFC 9396) | hand-rolled (`auth/rar.py`, `policy.py`) — small predicate, no library |
| Streaming HTTP transport | `mcp` SDK (`StreamableHTTPSessionManager`, P4b) |
| stdio transport | `mcp` SDK (`stdio_server`, P1) |
| JWT | `pyjwt[crypto]` (P4b) |
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
```

Update the existing `## Testing` section's line about the integration
suite to mention the new fixture files (`stack-introspection.yaml`,
`stack-policy-*.yaml`, `stack-oauth.yaml`) alongside `stack.yaml`.

- [ ] **Step 2: Create the future-work document**

Create `docs/superpowers/specs/future-work.md`:

```markdown
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
```

- [ ] **Step 3: Amend the main design spec's phasing table**

In `docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md`'s §12 phasing
table, insert a row between the `P4` and `P5` rows:

```markdown
| **P4.1** | `Dockerfile.base`, `docker-bake.hcl`, `mocks/*` auth, `tests/integration/fixtures/*` | Every phase P1–P4 has a Docker-driven integration test proving its headline capability, including real backend-side auth enforcement. |
```

- [ ] **Step 4: Add the blocking note to the P4-inbound plan**

In `docs/superpowers/plans/2026-09-22-mcp-sidekit-p4-inbound.md`, add a new
bullet to the `## Global Constraints` section (near the top, before Task 1):

```markdown
- **Blocking, before Task 1: inbound API-key auth needs an explicit design
  pass that this plan does not currently contain.** `auth.inbound` as
  designed here only covers OAuth bearer-JWT validation. Whether an API-key
  alternative is mutually exclusive with OAuth per transport, or both can
  be enabled at once, and where a per-key identity/`authorization_details`
  mapping lives, is undecided — resolve it (a short brainstorming pass,
  amending this plan's config-model task) before starting Task 1.
- **Non-blocking suggestion:** Task 9's end-to-end test currently sketches
  a hand-built fake IdP via `httpx.MockTransport`. Consider using the same
  `navikt/mock-oauth2-server` Docker pattern P4.1 establishes instead, for
  consistency with the rest of the Docker/mocks testing standard — evaluate
  when this task is actually executed.
```

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md docs/superpowers/specs/future-work.md \
        docs/superpowers/specs/2026-09-19-mcp-sidekit-design.md \
        docs/superpowers/plans/2026-09-22-mcp-sidekit-p4-inbound.md
git commit -m "docs: Docker/mocks testing standard, future-work tracking, P4.1 phasing amendment"
```

---

## Self-Review Notes

- **Spec coverage:** §2 (mocks standard, orders API key, billing OAuth) →
  Tasks 2, 5, 6. §3 (`Dockerfile.base`/Bake) → Task 1. §4 (P4.1 phasing,
  retrofit tasks, P4-inbound notes) → Tasks 1–6 collectively plus Task 6
  Step 3–4. §5 (future-work.md) → Task 6 Step 2. §6 (AGENTS.md tech table)
  → Task 6 Step 1. §7 (testing) — each task's own Docker test is that
  task's testing story; no separate task needed. §8 (rationale) is argued
  from throughout, not a separate task.
- **Version-pin risk, named explicitly rather than glossed over:** Task 5's
  `navikt/mock-oauth2-server:2.1.10` tag and its `JSON_CONFIG` shape are
  this plan's best-effort read of that project's documented behavior, not
  independently verified against a live instance while writing this plan —
  Task 5's steps say so and tell the implementer to confirm/adjust against
  the pinned version's current docs rather than treat a mismatch as their
  own bug.
