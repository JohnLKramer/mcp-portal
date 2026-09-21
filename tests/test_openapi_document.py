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


def test_parse_document_rejects_invalid_yaml():
    with pytest.raises(OpenApiError):
        parse_document("key: value\n  bad: [1,2\n", is_yaml=True)


def test_parse_document_rejects_a_document_with_no_openapi_field():
    with pytest.raises(OpenApiError) as exc:
        parse_document("{}", is_yaml=False)
    assert "openapi" in str(exc.value)


def test_parse_document_with_require_openapi_field_false_accepts_a_fragment():
    doc = parse_document('{"Foo": {"type": "string"}}', is_yaml=False, require_openapi_field=False)
    assert doc == {"Foo": {"type": "string"}}


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


def test_a_non_object_first_server_is_an_error():
    with pytest.raises(OpenApiError):
        resolve_base_url({"servers": ["not-an-object"]}, None)


def test_fetch_text_with_require_containment_rejects_a_relative_escape(tmp_path: Path):
    (tmp_path / "openapi.json").write_text(MINIMAL_JSON)
    (tmp_path.parent / "outside.json").write_text(MINIMAL_JSON)
    try:
        with pytest.raises(OpenApiError):
            fetch_text("../outside.json", tmp_path, httpx.Client(), require_containment=True)
    finally:
        (tmp_path.parent / "outside.json").unlink()


def test_fetch_text_with_require_containment_rejects_an_absolute_path(tmp_path: Path):
    outside = tmp_path.parent / "outside.json"
    outside.write_text(MINIMAL_JSON)
    try:
        with pytest.raises(OpenApiError):
            fetch_text(str(outside), tmp_path, httpx.Client(), require_containment=True)
    finally:
        outside.unlink()


def test_fetch_text_with_require_containment_allows_a_file_within_base_dir(tmp_path: Path):
    (tmp_path / "common.json").write_text(MINIMAL_JSON)
    text, is_yaml = fetch_text("common.json", tmp_path, httpx.Client(), require_containment=True)
    assert text == MINIMAL_JSON
