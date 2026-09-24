"""Docker integration test: introspect-safe (and introspect-unsafe) mode
against a live GraphQL endpoint served by the graphql mock. Proves the whole
GraphQL pipeline — introspection, entity/field-tier selection generation
(including `type_policy` field exclusion), classification, and execution
(including the `errors[]`-means-failure rule) — works together against a
real HTTP server, mirroring `test_stack_introspection.py` for the OpenAPI
path.
"""

from pathlib import Path

import pytest

from mcp_portal.app import build_app
from mcp_portal.config.loader import load_config

pytestmark = pytest.mark.integration

STACK_CONFIG = Path(__file__).resolve().parent / "fixtures" / "stack-graphql.yaml"
STACK_CONFIG_UNSAFE = Path(__file__).resolve().parent / "fixtures" / "stack-graphql-unsafe.yaml"


@pytest.mark.anyio
async def test_introspected_graphql_query_round_trips_through_the_mock(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        names = [t.name for t in app.invoker.tools()]
        assert "user" in names

        result = await app.invoker.call("user", {"id": "u1"})
        assert result.is_error is False
        assert "Ada" in result.content[0].text
        assert "ssn" not in result.content[0].text  # type_policy excluded it
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_graphql_mutation_is_action_and_round_trips(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG_UNSAFE))
    try:
        names = [t.name for t in app.invoker.tools()]
        assert "createuser" in names

        result = await app.invoker.call("createuser", {"name": "Grace"})
        assert result.is_error is False
        assert "Grace" in result.content[0].text
    finally:
        await app.aclose()


@pytest.mark.anyio
async def test_unknown_graphql_user_id_surfaces_as_a_tool_error(mock_stack: None):
    app = build_app(load_config(STACK_CONFIG))
    try:
        result = await app.invoker.call("user", {"id": "does-not-exist"})
        assert result.is_error is True
    finally:
        await app.aclose()
