"""Produce the credential to attach to upstream requests.

This module never learns which protocol it is authorizing — it yields a Credential
and the transport attaches it. P1 implements `none` and `static`; P4 adds
client_credentials and token exchange behind the same return type.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.auth.token_cache import TokenCache
from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import OutboundConfig
from mcp_portal.transports.http import Credential

_EXPIRY_LEEWAY_S = 60.0


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


class OutboundError(Exception):
    """Raised when acquiring a dynamic outbound credential fails."""


def _authorization_details_json(details: tuple[AuthorizationDetail, ...]) -> str | None:
    if not details:
        return None
    return json.dumps(
        [
            {
                k: v
                for k, v in {
                    "type": d.type,
                    "actions": list(d.actions) or None,
                    "locations": list(d.locations) or None,
                    "datatypes": list(d.datatypes) or None,
                    "identifier": d.identifier,
                    "privileges": list(d.privileges) or None,
                }.items()
                if v is not None
            }
            for d in details
        ]
    )


async def _post_token_request(
    client: httpx.AsyncClient,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    data: dict[str, str],
) -> tuple[str, float]:
    """POST a token request with HTTP Basic client authentication.

    Returns `(access_token, ttl_seconds)`. `ttl_seconds` already has the
    expiry leeway subtracted, so cache consumers never re-derive it.
    """
    response = await client.post(
        token_endpoint,
        data=data,
        auth=(client_id, client_secret),
        headers={"Accept": "application/json"},
    )
    if response.status_code != 200:
        raise OutboundError(
            f"token request to {token_endpoint!r} failed with {response.status_code}: "
            f"{response.text[:500]}"
        )
    payload = response.json()
    token = payload.get("access_token")
    if not isinstance(token, str):
        raise OutboundError(f"token response from {token_endpoint!r} has no 'access_token'")
    expires_in = payload.get("expires_in", 300)
    ttl = max(0.0, float(expires_in) - _EXPIRY_LEEWAY_S)
    return token, ttl


@dataclass(frozen=True, slots=True)
class ClientCredentialsSource:
    """RFC 6749 client credentials grant, cached per upstream+scopes+carry."""

    outbound: OutboundConfig
    secrets: dict[str, str]
    client: httpx.AsyncClient
    cache: TokenCache
    upstream_key: str

    def _client_secret(self) -> str:
        assert self.outbound.client_secret is not None
        try:
            return self.secrets[self.outbound.client_secret]
        except KeyError:
            raise OutboundError(
                f"secret reference {self.outbound.client_secret!r} was not resolved at load time"
            ) from None

    async def get(
        self, carry: tuple[AuthorizationDetail, ...], subject_token: str | None = None
    ) -> Credential | None:
        scopes = tuple(sorted(self.outbound.scopes))
        key = f"client_credentials:{self.upstream_key}:{scopes}:{hash(carry)}"

        async def fetch() -> tuple[str, float]:
            assert self.outbound.token_endpoint is not None
            assert self.outbound.client_id is not None
            data: dict[str, str] = {"grant_type": "client_credentials"}
            if scopes:
                data["scope"] = " ".join(scopes)
            details_json = _authorization_details_json(carry)
            if details_json is not None:
                data["authorization_details"] = details_json
            return await _post_token_request(
                self.client,
                self.outbound.token_endpoint,
                self.outbound.client_id,
                self._client_secret(),
                data,
            )

        token = await self.cache.get_or_fetch(key, fetch)
        return Credential(header="Authorization", value=f"Bearer {token}")
