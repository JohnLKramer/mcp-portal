"""Render survey/reconcile decisions into config- and policy-file-shaped
dicts, and write them to disk only after a diff is shown and confirmed
(§7 of the design).
"""

import difflib
import io
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from mcp_portal.configure.decisions import OperationDecision
from mcp_portal.configure.interaction import Prompter
from mcp_portal.operations import HttpBinding

YAML_SUFFIXES = frozenset({".yaml", ".yml"})


def render_config(
    decisions: Sequence[OperationDecision],
    *,
    server_name: str,
    upstream_base_urls: dict[str, str],
    mode: str = "configured",
) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for decision in decisions:
        if not decision.exposed:
            continue
        op = decision.operation
        assert isinstance(op.binding, HttpBinding)
        operations.append(
            {
                "id": op.id,
                "upstream": op.upstream,
                "description": op.description,
                "title": op.title,
                "group_tags": list(op.group_tags),
                "effect": decision.effect.value,
                "sensitivity": decision.sensitivity.value,
                "binding": {"method": op.binding.method, "path": op.binding.path},
            }
        )
    return {
        "version": "1",
        "mode": mode,
        "server": {"name": server_name, "transport": "stdio"},
        "upstreams": {key: {"base_url": url} for key, url in upstream_base_urls.items()},
        "operations": operations,
    }


def render_policy(decisions: Sequence[OperationDecision]) -> dict[str, Any]:
    rules: list[dict[str, Any]] = []
    for decision in decisions:
        if not decision.exposed or decision.require is None:
            continue
        rules.append(
            {
                "match": {"ids": [decision.operation.id]},
                "require": {
                    "authorization_details": [
                        {
                            "type": decision.require.type,
                            **(
                                {"actions": list(decision.require.actions)}
                                if decision.require.actions
                                else {}
                            ),
                        }
                    ]
                },
            }
        )
    return {"version": "1", "rules": rules}


def _render_yaml(data: dict[str, Any]) -> str:
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    stream = io.StringIO()
    yaml.dump(data, stream)
    return stream.getvalue()


def _render_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


def _round_trip_yaml(existing_text: str, data: dict[str, Any]) -> str:
    """Load the existing file with the comment/order/anchor-preserving
    round-trip loader, overwrite its top-level keys with the freshly
    rendered ones, and dump it back — so a hand-written comment on an
    untouched key (or the file's own header comment) survives a reconcile
    pass. New top-level keys are appended in insertion order."""
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    doc = yaml.load(existing_text)
    if doc is None:
        doc = {}
    for key, value in data.items():
        doc[key] = value
    stream = io.StringIO()
    yaml.dump(doc, stream)
    return stream.getvalue()


def render_text(path: Path, data: dict[str, Any]) -> tuple[str, str]:
    """Returns `(new_text, fidelity_message)`."""
    is_yaml = path.suffix in YAML_SUFFIXES
    if path.exists():
        existing = path.read_text()
        if is_yaml:
            return _round_trip_yaml(existing, data), (
                "YAML: comments, key order, and anchors in the existing file are preserved."
            )
        return _render_json(data), (
            "JSON: formatting is normalized (JSON cannot carry comments); key order is preserved."
        )
    return (
        _render_yaml(data) if is_yaml else _render_json(data)
    ), "new file: no prior formatting to preserve."


def write_with_confirmation(path: Path, rendered: dict[str, Any], prompter: Prompter) -> bool:
    new_text, fidelity_message = render_text(path, rendered)
    old_text = path.read_text() if path.exists() else ""

    print(f"-- {path} ({fidelity_message}) --")
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=str(path) if path.exists() else "/dev/null",
        tofile=str(path),
    )
    print("".join(diff) or "(no changes)")

    if not prompter.confirm(f"write {path}?", default=True):
        return False

    path.write_text(new_text)
    return True
