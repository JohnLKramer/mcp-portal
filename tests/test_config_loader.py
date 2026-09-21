import json
from pathlib import Path

import pytest

from mcp_portal.config.loader import (
    ConfigError,
    check_operation_headers,
    credential_headers_for,
    load_config,
    resolve_secret,
)
from mcp_portal.config.models import Config
from mcp_portal.operations import (
    Effect,
    HttpBinding,
    Operation,
    Parameter,
    ParamLocation,
    Sensitivity,
)

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
                "auth": {
                    "outbound": {"mode": "static", "header": "X-Api-Key", "value": "${env:K}"}
                },
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
