"""The calling identity a policy decision is evaluated against.

Per §8 of the design, `transport: stdio` always uses the locally-asserted
principal built here. `transport: http` (P4) additionally derives one from a
validated bearer token, or falls back to this same local principal when
inbound auth is disabled — this module does not need to know which case
applies, since `Principal` is transport-agnostic.
"""

from dataclasses import dataclass

from mcp_portal.auth.rar import AuthorizationDetail, RarError, parse_authorization_details
from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import LocalPrincipalConfig


@dataclass(frozen=True, slots=True)
class Principal:
    identity: str
    authorization_details: tuple[AuthorizationDetail, ...]


def local_principal(config: LocalPrincipalConfig) -> Principal:
    """Build the self-asserted principal from `auth.local_principal` config.

    What this guardrail is honestly for: the threat model under stdio is not
    a malicious operator, it is an over-eager agent. Constraining which
    tools a model may invoke is worthwhile even though the human launching
    the process can trivially bypass it by editing the config directly — that
    is a guardrail, not a security boundary.
    """
    try:
        details = parse_authorization_details(config.authorization_details)
    except RarError as exc:
        raise ConfigError(f"auth.local_principal.authorization_details: {exc}") from exc
    return Principal(identity="local", authorization_details=details)
