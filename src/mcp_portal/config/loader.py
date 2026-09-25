"""Load, resolve, and validate a config file.

Every check here runs at startup. A config that would fail on some future tool
call is a config that fails to load.
"""

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from mcp_portal.config.models import SECRET_REF_PATTERN, McpPortalConfig, PolicyFileConfig
from mcp_portal.config.policy import PolicyConfig
from mcp_portal.operations import HttpBinding, Operation, ParamLocation

_SECRET_RE = re.compile(SECRET_REF_PATTERN)

# A model-supplied Authorization header would bypass the outbound broker entirely,
# turning the gateway into an open proxy for whatever credential the model invents.
# The rest are hop-by-hop or routing headers that let a caller reshape the request.
DENYLISTED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "host",
        "cookie",
        "set-cookie",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "trailer",
        "expect",
    }
)
DENYLISTED_PREFIXES = ("proxy-", "x-forwarded-")


class ConfigError(Exception):
    """Raised for any configuration problem. Always at startup, never at call time."""


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: McpPortalConfig
    base_dir: Path
    secrets: dict[str, str]
    policy: PolicyConfig | None = None


def resolve_secret(ref: str, base_dir: Path) -> str:
    """Resolve a ${env:VAR} or ${file:path} reference.

    File paths resolve against the config file's directory, not the process CWD,
    so a config behaves identically regardless of where it is launched from.
    """
    if not _SECRET_RE.fullmatch(ref):
        raise ConfigError(f"not a secret reference: {ref!r}")

    kind, _, target = ref[2:-1].partition(":")

    if kind == "env":
        value = os.environ.get(target)
        if value is None:
            raise ConfigError(f"environment variable {target!r} is not set")
        return value

    path = Path(target)
    resolved = path if path.is_absolute() else base_dir / path
    try:
        text = resolved.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read secret file {resolved}: {exc}") from exc
    return text[:-1] if text.endswith("\n") else text


def is_denylisted(header: str, extra: frozenset[str]) -> bool:
    lowered = header.lower()
    return (
        lowered in DENYLISTED_HEADERS or lowered in extra or lowered.startswith(DENYLISTED_PREFIXES)
    )


def check_header_denylist(config: McpPortalConfig) -> None:
    """Reject bindings that let a caller set a security-relevant header."""
    credential_headers = frozenset(
        u.auth.outbound.header.lower()
        for u in config.upstreams.values()
        if u.auth.outbound.mode != "none"
    )
    for op in config.operations:
        for param in op.binding.parameters:
            if param.location is not ParamLocation.HEADER:
                continue
            header = param.wire_name or param.arg
            if is_denylisted(header, credential_headers):
                raise ConfigError(
                    f"operation {op.id!r} declares header parameter {header!r}, "
                    "which is denylisted: a caller-supplied value here can bypass "
                    "the outbound credential or reshape the request"
                )


def credential_headers_for(config: McpPortalConfig) -> frozenset[str]:
    return frozenset(
        u.auth.outbound.header.lower()
        for u in config.upstreams.values()
        if u.auth.outbound.mode != "none"
    )


def check_operation_headers(
    operations: Iterable[Operation], credential_headers: frozenset[str]
) -> None:
    """The same denylist as `check_header_denylist`, applied to any Operation list.

    Explicit entries are checked here a second time as a side effect of the
    caller merging both sources before this runs; that redundancy is harmless.
    """
    for op in operations:
        if not isinstance(op.binding, HttpBinding):
            continue
        for param in op.binding.parameters:
            if param.location is not ParamLocation.HEADER:
                continue
            if is_denylisted(param.wire_name, credential_headers):
                raise ConfigError(
                    f"operation {op.id!r} declares header parameter {param.wire_name!r}, "
                    "which is denylisted: a caller-supplied value here can bypass "
                    "the outbound credential or reshape the request"
                )


def _collect_secrets(data: Any, base_dir: Path, out: dict[str, str]) -> None:
    if isinstance(data, str):
        if _SECRET_RE.fullmatch(data):
            out[data] = resolve_secret(data, base_dir)
    elif isinstance(data, dict):
        for value in data.values():
            _collect_secrets(value, base_dir, out)
    elif isinstance(data, list):
        for value in data:
            _collect_secrets(value, base_dir, out)


def _read(path: Path) -> Any:
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc

    if path.suffix in {".yaml", ".yml"}:
        return YAML(typ="safe").load(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc


def load_policy_file(policy: PolicyFileConfig, base_dir: Path) -> PolicyConfig:
    """Load and validate the RAR policy file, resolved relative to `base_dir` —
    the directory containing the *main* config file, per §5, so a config
    behaves identically regardless of where the process is launched from."""
    path = Path(policy.file)
    resolved = path if path.is_absolute() else base_dir / path
    raw = _read(resolved)
    try:
        return PolicyConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid policy file {resolved}:\n{exc}") from exc


def load_config(path: Path) -> LoadedConfig:
    path = path.resolve()
    base_dir = path.parent
    raw = _read(path)

    try:
        config = McpPortalConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config {path}:\n{exc}") from exc

    check_header_denylist(config)

    secrets: dict[str, str] = {}
    _collect_secrets(raw, base_dir, secrets)

    policy = load_policy_file(config.policy, base_dir) if config.policy is not None else None

    return LoadedConfig(config=config, base_dir=base_dir, secrets=secrets, policy=policy)
