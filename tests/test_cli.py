import json
import logging
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


def test_duplicate_operation_ids_report_a_config_error_without_a_traceback(tmp_path: Path, capsys):
    broken = CONFIG | {"operations": CONFIG["operations"] * 2}
    code = main(["validate", "--config", str(write(tmp_path, broken))])
    err = capsys.readouterr().err
    assert code == 2
    assert "configuration error:" in err
    assert "list_invoices" in err


def test_two_operations_declaring_one_tool_name_report_a_config_error(tmp_path: Path, capsys):
    broken = CONFIG | {
        "operations": [
            CONFIG["operations"][0] | {"id": "a", "name": "invoices"},
            CONFIG["operations"][0] | {"id": "b", "name": "invoices"},
        ]
    }
    code = main(["validate", "--config", str(write(tmp_path, broken))])
    err = capsys.readouterr().err
    assert code == 2
    assert "configuration error:" in err
    assert "invoices" in err


def test_missing_config_file_exits_nonzero(tmp_path: Path, capsys):
    code = main(["validate", "--config", str(tmp_path / "absent.json")])
    assert code == 2


def test_logging_setup_does_not_switch_on_httpx_request_logging(tmp_path: Path):
    # httpx logs one line per request at INFO containing the full URL, query
    # arguments included. Configuring the root logger would publish those.
    main(["validate", "--config", str(write(tmp_path, CONFIG))])
    assert logging.getLogger("mcp_portal").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


def test_serve_requires_a_config(capsys):
    with pytest.raises(SystemExit):
        main(["serve"])


def test_serve_dispatches_to_http_when_transport_is_http(tmp_path, monkeypatch):
    """Not a full server-startup test (that needs a live port + uvicorn
    event loop, covered by the integration suite) — asserts main() picks the
    HTTP path rather than stdio by monkeypatching uvicorn's runner and
    checking it was invoked with the built ASGI app."""
    calls = []

    async def fake_serve(app, host, port):
        calls.append((app, host, port))

    import mcp_portal.cli as cli_module

    monkeypatch.setattr(cli_module, "_run_uvicorn", fake_serve)

    config = CONFIG | {
        "server": {"name": "s", "transport": "http", "http": {"host": "127.0.0.1", "port": 8000}}
    }
    main(["serve", "--config", str(write(tmp_path, config))])
    assert len(calls) == 1
