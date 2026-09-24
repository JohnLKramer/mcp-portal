from pathlib import Path

import httpx
import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

EXAMPLES = sorted((Path(__file__).resolve().parents[1] / "examples").glob("*.yaml"))

# examples/graphql.yaml has a live-looking `introspection.graphql.url`; this
# stands in for the real server so the test never makes a network call.
_GRAPHQL_SCHEMA_RESPONSE = {
    "data": {
        "__schema": {
            "queryType": {"name": "Query"},
            "mutationType": None,
            "types": [
                {
                    "kind": "OBJECT",
                    "name": "Query",
                    "fields": [
                        {
                            "name": "product",
                            "description": "Fetch a product by id.",
                            "args": [
                                {
                                    "name": "id",
                                    "type": {
                                        "kind": "NON_NULL",
                                        "name": None,
                                        "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None},
                                    },
                                }
                            ],
                            "type": {"kind": "SCALAR", "name": "String", "ofType": None},
                        }
                    ],
                }
            ],
        }
    }
}


def test_examples_directory_is_not_empty():
    assert EXAMPLES


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
@pytest.mark.anyio
async def test_every_example_config_loads_and_builds(path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BILLING_API_KEY", "sk-test")
    monkeypatch.setenv("CATALOG_API_KEY", "sk-test")
    if path.name == "graphql.yaml":

        def fake_client_post(self: httpx.Client, url: str, json: object = None, **kwargs: object):
            return httpx.Response(
                200, json=_GRAPHQL_SCHEMA_RESPONSE, request=httpx.Request("POST", url)
            )

        monkeypatch.setattr(httpx.Client, "post", fake_client_post)
    app = build_app(load_config(path))
    try:
        assert app.invoker.tools()
    finally:
        await app.aclose()
