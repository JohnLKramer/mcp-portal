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
