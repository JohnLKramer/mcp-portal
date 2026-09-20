"""Produce the credential to attach to upstream requests.

This module never learns which protocol it is authorizing — it yields a Credential
and the transport attaches it. P1 implements `none` and `static`; P4 adds
client_credentials and token exchange behind the same return type.
"""

from collections.abc import Mapping

from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import OutboundConfig
from mcp_portal.transports.http import Credential


def credential_for(outbound: OutboundConfig, secrets: Mapping[str, str]) -> Credential | None:
    if outbound.mode == "none":
        return None

    ref = outbound.value
    if ref is None:
        raise ConfigError("outbound mode 'static' requires 'value'")
    try:
        secret = secrets[ref]
    except KeyError:
        raise ConfigError(f"secret reference {ref!r} was not resolved at load time") from None

    value = f"{outbound.scheme} {secret}" if outbound.scheme else secret
    return Credential(header=outbound.header, value=value)
