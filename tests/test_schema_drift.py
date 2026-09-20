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
