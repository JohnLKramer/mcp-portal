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

from mcp_portal.config.policy import PolicyConfig, PolicyRule
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
    # Sorted by id so a no-op second `configure` run produces no diff: the
    # survey and reconcile pipelines hand back different orderings (tag-sorted
    # vs unchanged-then-surveyed) for the same set of decisions.
    for decision in sorted(decisions, key=lambda d: d.operation.id):
        if not decision.exposed:
            continue
        op = decision.operation
        if not isinstance(op.binding, HttpBinding):
            raise TypeError(
                f"operation {op.id!r} has a non-HTTP binding, which configure cannot render yet"
            )
        parameters = [
            {
                "arg": p.arg,
                "in": p.location.value,
                "wire_name": p.wire_name,
                "required": p.required,
                "schema": dict(p.schema),
                "style": p.style,
                "explode": p.explode,
            }
            for p in op.binding.parameters
        ]
        binding_dict: dict[str, Any] = {
            "method": op.binding.method,
            "path": op.binding.path,
            "parameters": parameters,
        }
        if op.binding.body is not None:
            binding_dict["body"] = {
                "content_type": op.binding.body.content_type,
                "mode": op.binding.body.mode.value,
                "schema": dict(op.binding.body.schema),
            }
        entry: dict[str, Any] = {
            "id": op.id,
            "upstream": op.upstream,
            "description": op.description,
            "title": op.title,
            "group_tags": list(op.group_tags),
            "effect": decision.effect.value,
            "sensitivity": decision.sensitivity.value,
            "binding": binding_dict,
        }
        if op.name:
            entry["name"] = op.name
        operations.append(entry)
    return {
        "version": "1",
        "mode": mode,
        "server": {"name": server_name, "transport": "stdio"},
        "upstreams": {key: {"base_url": url} for key, url in upstream_base_urls.items()},
        "operations": operations,
    }


def render_policy(
    decisions: Sequence[OperationDecision],
    existing_policy: PolicyConfig | None = None,
) -> dict[str, Any]:
    """Render the policy file, preserving every hand-authored rule untouched.

    A rule is "configure-owned" — and therefore safe to regenerate — only
    when BOTH of the following hold:

    1. Its `match` is an exact single-id match (`ids=[op_id]`, nothing else)
       for an operation `configure` is currently managing.
    2. Its existing content is fully representable by `RequiredDetail`
       (`configure`'s own requirement model, which carries only `type` and
       `actions`) — i.e. `outbound.carry` is `False` and `require` is either
       absent or has exactly one `authorization_details` entry using only
       `type`/`actions`.

    Every other rule (tag/upstream/effect/sensitivity-matched, multi-id,
    matching an id `configure` doesn't know about, or matching by id but
    carrying content `configure` cannot express, such as `outbound.carry`,
    `locations`, `datatypes`, `identifier`, `privileges`, or multiple
    `authorization_details` entries) is preserved verbatim: this is the only
    way to keep the module-level invariant in `config/policy.py`
    ("authorization policy must never be silently regenerated from a third
    party's schema") true across a `configure` write.
    """
    decision_ids = {d.operation.id for d in decisions}

    def _within_required_detail_shape(rule: PolicyRule) -> bool:
        """Whether this rule's content is fully representable by
        `RequiredDetail` (type + actions only, no `outbound.carry`,
        `locations`, `datatypes`, `identifier`, or `privileges`, and at
        most one `authorization_details` entry). A rule that isn't is
        preserved as foreign even if its `match` looks configure-shaped —
        regenerating it would silently drop content `configure` cannot
        express, which is the authorization-loosening this guard exists
        to prevent.
        """
        if rule.outbound.carry:
            return False
        if rule.require is None:
            return True
        details = rule.require.authorization_details
        if len(details) != 1:
            return False
        detail = details[0]
        return not (detail.locations or detail.datatypes or detail.identifier or detail.privileges)

    def _matches_configure_id_shape(rule: PolicyRule) -> bool:
        match = rule.match
        return (
            len(match.ids) == 1
            and match.ids[0] in decision_ids
            and not match.tags
            and not match.upstream
            and not match.effect
            and not match.sensitivity
        )

    def _is_configure_owned(rule: PolicyRule) -> bool:
        return _matches_configure_id_shape(rule) and _within_required_detail_shape(rule)

    foreign_rules: list[dict[str, Any]] = []
    # Ids whose only reason for being foreign is that their content is
    # richer than `RequiredDetail` can represent: `configure` must not also
    # emit its own generated rule for these ids, or the id would end up
    # matched by two rules in the rendered policy.
    foreign_id_shaped_ids: set[str] = set()
    if existing_policy is not None:
        for rule in existing_policy.rules:
            if not _is_configure_owned(rule):
                foreign_rules.append(rule.model_dump(mode="json", by_alias=True, exclude_none=True))
                if _matches_configure_id_shape(rule):
                    foreign_id_shaped_ids.add(rule.match.ids[0])

    owned_rules: list[dict[str, Any]] = []
    for decision in decisions:
        if not decision.exposed or decision.require is None:
            continue
        if decision.operation.id in foreign_id_shaped_ids:
            continue
        owned_rules.append(
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

    result: dict[str, Any] = {"version": "1", "rules": foreign_rules + owned_rules}
    if existing_policy is not None:
        result["defaults"] = {"unmatched": existing_policy.defaults.unmatched}
    return result


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

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text)
    return True
