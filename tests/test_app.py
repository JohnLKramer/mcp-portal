import json
import logging
import shutil
from pathlib import Path

import httpx
import pytest

from mcp_portal.app import build_app
from mcp_portal.auth.outbound import ClientCredentialsSource
from mcp_portal.config.loader import ConfigError, load_config

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"

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


def _write_config(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
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


@pytest.mark.anyio
async def test_the_configured_credential_reaches_the_outbound_request(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"invoices": []})

    # build_app owns its clients, so the mock transport is injected at construction.
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    app = build_app(load_config(config_path))
    try:
        result = await app.invoker.call("billing_list_invoices", {"customer_id": "cus_1"})
    finally:
        await app.aclose()

    assert result.is_error is False
    # `${env:BILLING_KEY}` resolved to "sk-test" and the Bearer scheme was applied.
    assert seen[0].headers["authorization"] == "Bearer sk-test"


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
async def test_a_relative_document_server_url_fails_as_a_config_error(tmp_path: Path):
    # billing.json's servers[] entry is replaced with a relative URL ("/v1"), the
    # kind real OpenAPI documents legitimately declare. With no operator-set
    # `base_url` to override it, this must fail at load time as a ConfigError,
    # not sail through and blow up every call with httpx.UnsupportedProtocol.
    shutil.copy(FIXTURES / "relative_servers.json", tmp_path / "relative_servers.json")
    config_path = _write_config(
        tmp_path,
        {
            "version": "1",
            "mode": "introspect-safe",
            "server": {"name": "s", "transport": "stdio"},
            "upstreams": {
                "billing": {"introspection": {"openapi": {"file": "relative_servers.json"}}}
            },
        },
    )
    with pytest.raises(ConfigError, match="not an absolute http"):
        build_app(load_config(config_path))


@pytest.mark.anyio
async def test_mixed_introspected_and_explicit_only_upstreams_both_serve_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # One upstream introspects an OpenAPI document; a second upstream has no
    # introspection at all, only `base_url` and an explicit operation. Both
    # must end up in the final tool set, and calling each tool must reach the
    # correct upstream's transport (proving the two upstreams weren't merged
    # onto a single base_url).
    shutil.copy(FIXTURES / "billing.json", tmp_path / "billing.json")
    config_path = _write_config(
        tmp_path,
        {
            "version": "1",
            "mode": "introspect-safe",
            "server": {"name": "s", "transport": "stdio"},
            "upstreams": {
                "billing": {"introspection": {"openapi": {"file": "billing.json"}}},
                "payments": {"base_url": "https://payments.example.com"},
            },
            "operations": [
                {
                    "id": "list_payments",
                    "upstream": "payments",
                    "description": "List payments.",
                    "binding": {"method": "GET", "path": "/payments"},
                }
            ],
        },
    )

    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    app = build_app(load_config(config_path))
    try:
        names = {t.name for t in app.invoker.tools()}
        assert "list_invoices" in names
        assert "list_payments" in names

        await app.invoker.call("list_invoices", {})
        await app.invoker.call("list_payments", {})
    finally:
        await app.aclose()

    hosts = {str(request.url.host) for request in seen}
    assert hosts == {"api.example.com", "payments.example.com"}


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


@pytest.mark.anyio
async def test_build_app_with_no_policy_file_allows_every_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Reuses this file's existing `_write_config`/`CONFIG`-style helper
    # with no `policy` key — behavior must be identical to P1/P2. Exercised
    # through the real `ToolInvoker.call()` path, not by touching internals.
    monkeypatch.setenv("BILLING_KEY", "sk-test")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"invoices": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    loaded = load_config(_write_config(tmp_path, CONFIG))
    app = build_app(loaded)
    try:
        result = await app.invoker.call("billing_list_invoices", {"customer_id": "cus_1"})
    finally:
        await app.aclose()
    assert result.is_error is False


def test_build_app_wires_the_configured_local_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("BILLING_KEY", "sk-test")
    payload = CONFIG | {
        "auth": {
            "local_principal": {
                "authorization_details": [{"type": "payment_initiation", "actions": ["initiate"]}]
            }
        }
    }
    loaded = load_config(_write_config(tmp_path, payload))
    app = build_app(loaded)
    assert app.invoker._principal.authorization_details[0].type == "payment_initiation"


@pytest.mark.anyio
async def test_build_app_wires_a_policy_file_and_enforces_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A denied call must never reach the transport — this test does not mock
    # httpx.AsyncClient at all, so if enforcement failed to deny the call and
    # a real request were attempted, it would fail loudly rather than pass
    # by accident.
    monkeypatch.setenv("BILLING_KEY", "sk-test")
    (tmp_path / "rar-policy.yaml").write_text(
        "version: '1'\n"
        "defaults: {unmatched: deny}\n"
        "rules:\n"
        "  - match: {upstream: [billing]}\n"
        "    require: {authorization_details: [{type: payment_initiation}]}\n"
    )
    payload = CONFIG | {"policy": {"file": "./rar-policy.yaml"}}
    loaded = load_config(_write_config(tmp_path, payload))
    app = build_app(loaded)
    try:
        result = await app.invoker.call("billing_list_invoices", {"customer_id": "cus_1"})
    finally:
        await app.aclose()
    assert result.is_error is True
    assert "payment_initiation" in result.content[0].text


@pytest.mark.anyio
async def test_client_credentials_upstream_builds_a_dynamic_credential_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("BILLING_CLIENT_SECRET", "shh")
    payload = CONFIG | {
        "upstreams": {
            "billing": {
                "base_url": "https://api.example.com",
                "auth": {
                    "outbound": {
                        "mode": "client_credentials",
                        "token_endpoint": "https://idp.example.com/oauth2/token",
                        "client_id": "sidekit-billing",
                        "client_secret": "${env:BILLING_CLIENT_SECRET}",
                    }
                },
            }
        }
    }
    loaded = load_config(_write_config(tmp_path, payload))
    app = build_app(loaded)
    try:
        transport = app.invoker._transports["billing"]
        assert isinstance(transport._credential_source, ClientCredentialsSource)
    finally:
        await app.aclose()


def test_build_app_logs_a_dead_policy_rule_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    monkeypatch.setenv("BILLING_KEY", "sk-test")
    (tmp_path / "rar-policy.yaml").write_text(
        "version: '1'\n"
        "rules:\n"
        "  - match: {tags: [nonexistent]}\n"
        "    require: {authorization_details: [{type: x}]}\n"
    )
    payload = CONFIG | {"policy": {"file": "./rar-policy.yaml"}}
    with caplog.at_level(logging.WARNING, logger="mcp_portal"):
        build_app(load_config(_write_config(tmp_path, payload)))
    assert "nonexistent" in caplog.text
