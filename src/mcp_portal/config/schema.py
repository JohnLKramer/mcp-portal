"""Emit the published JSON Schema from the Pydantic models.

A hand-maintained schema drifts from the code that reads it, and a drifted schema
on a public contract is worse than no schema. CI runs test_schema_drift.py.
"""

import json
from pathlib import Path
from typing import Any

from mcp_portal.config.models import McpPortalConfig

SCHEMA_ID = "https://schemas.mcp-portal.dev/config-v1.schema.json"
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "config-v1.schema.json"


def config_json_schema() -> dict[str, Any]:
    schema = McpPortalConfig.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = SCHEMA_ID
    schema["title"] = "mcp-portal configuration"
    return schema


def write_schema() -> Path:
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(json.dumps(config_json_schema(), indent=2) + "\n")
    return SCHEMA_PATH


if __name__ == "__main__":
    print(f"wrote {write_schema()}")
