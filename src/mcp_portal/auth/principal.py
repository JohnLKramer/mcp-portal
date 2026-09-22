"""The calling identity a policy decision is evaluated against.

Per §8 of the design, `transport: stdio` always uses the locally-asserted
principal built here. `transport: http` (P4) additionally derives one from a
validated bearer token, or falls back to this same local principal when
inbound auth is disabled — this module does not need to know which case
applies, since `Principal` is transport-agnostic.
"""

from dataclasses import dataclass

from mcp.server.auth.provider import AccessToken

from mcp_portal.auth.rar import AuthorizationDetail, RarError, parse_authorization_details
from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import LocalPrincipalConfig


@dataclass(frozen=True, slots=True)
class Principal:
    identity: str
    authorization_details: tuple[AuthorizationDetail, ...]
    subject_token: str | None = None
    subject_token_expires_at: int | None = None


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


def principal_from_access_token(access_token: AccessToken) -> Principal | None:
    """Build the principal a validated HTTP bearer token represents (§8).

    Returns `None` when `authorization_details` is present but malformed —
    the caller (the bearer-auth wiring in `server/http.py`) must deny the
    request rather than treat it as an absent claim, matching
    `JwtTokenVerifier`'s own rule for the same claim.
    """
    raw_details = (access_token.claims or {}).get("authorization_details", [])
    try:
        details = parse_authorization_details(raw_details)
    except RarError:
        return None
    identity = access_token.subject or access_token.client_id
    return Principal(
        identity=identity,
        authorization_details=details,
        subject_token=access_token.token,
        subject_token_expires_at=access_token.expires_at,
    )
