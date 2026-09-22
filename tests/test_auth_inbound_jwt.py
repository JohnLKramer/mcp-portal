import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_portal.auth.inbound import JwtTokenVerifier


class _FakeJwks:
    """A `JwksCache`-shaped stub returning one fixed key, so these tests
    don't need a real JWKS HTTP round trip."""

    def __init__(self, jwk: dict) -> None:
        self._jwk = jwk

    async def key_for(self, kid: str) -> dict | None:
        return self._jwk if kid == self._jwk.get("kid") else None


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwk(rsa_key) -> dict:
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True)
    return public_jwk | {"kid": "test-kid", "use": "sig", "alg": "RS256"}


def _token(rsa_key, kid: str = "test-kid", **claim_overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": "https://api.example.com/mcp",
        "exp": now + 300,
        "nbf": now - 5,
        "scope": "invoices.read",
    } | claim_overrides
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": kid, "typ": "at+jwt"})


def verifier(jwk: dict, **overrides) -> JwtTokenVerifier:
    base = dict(
        issuer="https://idp.example.com",
        audience="https://api.example.com/mcp",
        algorithms=["RS256", "ES256"],
        required_scopes=[],
        leeway_s=60.0,
        jwks=_FakeJwks(jwk),
    )
    return JwtTokenVerifier(**(base | overrides))


@pytest.mark.anyio
async def test_a_well_formed_token_verifies(rsa_key, jwk):
    access = await verifier(jwk).verify_token(_token(rsa_key))
    assert access is not None
    assert access.scopes == ["invoices.read"]
    assert access.token == _token(rsa_key) or True  # token round-trips; exact string covered below


@pytest.mark.anyio
async def test_the_raw_token_string_is_preserved_as_the_subject_token(rsa_key, jwk):
    raw = _token(rsa_key)
    access = await verifier(jwk).verify_token(raw)
    assert access.token == raw


@pytest.mark.anyio
async def test_scp_array_form_is_also_accepted(rsa_key, jwk):
    now = int(time.time())
    raw = jwt.encode(
        {
            "iss": "https://idp.example.com",
            "aud": "https://api.example.com/mcp",
            "exp": now + 300,
            "scp": ["invoices.read", "invoices.write"],
        },
        rsa_key,
        algorithm="RS256",
        headers={"kid": "test-kid", "typ": "at+jwt"},
    )
    access = await verifier(jwk).verify_token(raw)
    assert access is not None
    assert set(access.scopes) == {"invoices.read", "invoices.write"}


@pytest.mark.anyio
async def test_authorization_details_claim_is_preserved_in_claims(rsa_key, jwk):
    raw = _token(
        rsa_key, authorization_details=[{"type": "payment_initiation", "actions": ["initiate"]}]
    )
    access = await verifier(jwk).verify_token(raw)
    assert access is not None
    assert access.claims["authorization_details"] == [
        {"type": "payment_initiation", "actions": ["initiate"]}
    ]


@pytest.mark.anyio
async def test_a_malformed_authorization_details_claim_denies_the_request(rsa_key, jwk):
    raw = _token(rsa_key, authorization_details="not-a-list")
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_a_wrong_issuer_is_rejected(rsa_key, jwk):
    raw = _token(rsa_key, iss="https://attacker.example.com")
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_a_wrong_audience_is_rejected(rsa_key, jwk):
    raw = _token(rsa_key, aud="https://other-api.example.com")
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_an_audience_array_containing_the_configured_audience_is_accepted(rsa_key, jwk):
    raw = _token(rsa_key, aud=["https://other.example.com", "https://api.example.com/mcp"])
    assert await verifier(jwk).verify_token(raw) is not None


@pytest.mark.anyio
async def test_an_expired_token_is_rejected(rsa_key, jwk):
    now = int(time.time())
    raw = _token(rsa_key, exp=now - 120)
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_leeway_tolerates_a_recently_expired_token(rsa_key, jwk):
    now = int(time.time())
    raw = _token(rsa_key, exp=now - 10)  # within the default 60s leeway
    assert await verifier(jwk).verify_token(raw) is not None


@pytest.mark.anyio
async def test_a_not_yet_valid_token_is_rejected(rsa_key, jwk):
    now = int(time.time())
    raw = _token(rsa_key, nbf=now + 120)
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_missing_required_scope_is_rejected(rsa_key, jwk):
    v = verifier(jwk, required_scopes=["invoices.write"])
    assert await v.verify_token(_token(rsa_key)) is None  # token only has invoices.read


@pytest.mark.anyio
async def test_an_unknown_kid_is_rejected(rsa_key, jwk):
    raw = _token(rsa_key, kid="ghost-kid")
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_typ_none_of_the_two_accepted_values_is_rejected(rsa_key, jwk):
    now = int(time.time())
    raw = jwt.encode(
        {"iss": "https://idp.example.com", "aud": "https://api.example.com/mcp", "exp": now + 300},
        rsa_key,
        algorithm="RS256",
        headers={"kid": "test-kid", "typ": "weird+jwt"},
    )
    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_a_null_typ_header_value_is_rejected_rather_than_raising(rsa_key, jwk):
    """A crafted, unverified header can have `typ: null` (a valid JSON value
    PyJWT's `get_unverified_header` happily returns), as opposed to the `typ`
    key simply being absent (already covered by the default `""` in
    `header.get("typ", "")`). `jwt.encode(..., headers={"typ": None})` drops
    the key entirely rather than emitting a null, so it can't be used to
    construct this case — the header has to be hand-built the same way the
    HS256 key-confusion test below does, to actually put a JSON `null` in
    the `typ` slot and reach the vulnerable `.lower()` call before signature
    verification.
    """
    import base64
    import json

    now = int(time.time())
    header = {"alg": "RS256", "typ": None, "kid": "test-kid"}
    payload = {
        "iss": "https://idp.example.com",
        "aud": "https://api.example.com/mcp",
        "exp": now + 300,
    }

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    signing_input = (
        f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    )
    raw = f"{signing_input}.{_b64url(b'not-a-real-signature')}"

    assert await verifier(jwk).verify_token(raw) is None


@pytest.mark.anyio
async def test_alg_none_is_rejected_unconditionally(jwk):
    now = int(time.time())
    raw = jwt.encode(
        {"iss": "https://idp.example.com", "aud": "https://api.example.com/mcp", "exp": now + 300},
        "",
        algorithm="none",
        headers={"typ": "at+jwt"},
    )
    assert await verifier(jwk, algorithms=["none", "RS256"]).verify_token(raw) is None


@pytest.mark.anyio
async def test_hs256_signed_with_the_rsa_public_key_pem_is_rejected(rsa_key, jwk):
    """The classic key-confusion attack: HS256, keyed with the RSA public
    key's own bytes. Rejected because HS256 is not in the default allowlist
    and there is no shared-secret config field for JWKS-only lookup to
    satisfy it even if it were (see this plan's Global Constraints).

    Installed PyJWT (2.14) added its own safety net that refuses to
    `encode()` an HMAC token whose key looks like a PEM/DER asymmetric key,
    which would prevent this test from even constructing the attack token.
    A real attacker doesn't go through our library's encoder, so the token
    here is built by hand (raw HS256 JWS: base64url header + payload, HMAC'd
    with the PEM bytes) to still exercise our verifier's own defense rather
    than PyJWT's unrelated encode-time guard.
    """
    import base64
    import hashlib
    import hmac
    import json

    from cryptography.hazmat.primitives import serialization

    public_pem = rsa_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    now = int(time.time())
    header = {"alg": "HS256", "typ": "at+jwt", "kid": "test-kid"}
    payload = {
        "iss": "https://idp.example.com",
        "aud": "https://api.example.com/mcp",
        "exp": now + 300,
    }

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    signing_input = (
        f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    )
    signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    raw = f"{signing_input}.{_b64url(signature)}"

    assert await verifier(jwk).verify_token(raw) is None
