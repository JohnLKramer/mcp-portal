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
