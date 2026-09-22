import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from billing_mock.app import app


@pytest.fixture
def client():
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _bearer(rsa_key, **claim_overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "http://mock-oauth2-server:8080/default",
        "aud": "billing-mock",
        "exp": now + 300,
        "sub": "billing-service",
    } | claim_overrides
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test-kid"})


@pytest.fixture
def auth_headers(monkeypatch, rsa_key):
    """Every route but /healthz and /openapi.json now requires a bearer token
    (see test_list_invoices_without_a_bearer_token_is_401 below). Stub the JWKS
    lookup and hand back a header dict so pre-existing tests can keep exercising
    the routes without talking to a real mock-oauth2-server."""
    from billing_mock import app as app_module

    class _FakeSigningKey:
        key = rsa_key.public_key()

    class _FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    monkeypatch.setattr(app_module, "_jwks_client", _FakeJwksClient())
    return {"Authorization": f"Bearer {_bearer(rsa_key)}"}


def test_healthz_returns_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_list_invoices_requires_customer_id(client, auth_headers):
    response = client.get("/v1/invoices", headers=auth_headers)
    assert response.status_code == 400


def test_list_invoices_returns_seeded_data(client, auth_headers):
    response = client.get(
        "/v1/invoices", query_string={"customerId": "cust_1"}, headers=auth_headers
    )
    assert response.status_code == 200
    assert len(response.get_json()["invoices"]) == 2


def test_list_invoices_respects_limit(client, auth_headers):
    response = client.get(
        "/v1/invoices",
        query_string={"customerId": "cust_1", "limit": 1},
        headers=auth_headers,
    )
    assert len(response.get_json()["invoices"]) == 1


def test_get_tax_id_unknown_customer_is_404(client, auth_headers):
    response = client.get("/v1/customers/nope/tax-id", headers=auth_headers)
    assert response.status_code == 404


def test_get_tax_id_known_customer(client, auth_headers):
    response = client.get("/v1/customers/cust_1/tax-id", headers=auth_headers)
    assert response.status_code == 200
    assert response.get_json()["tax_id"] == "TAX-CUST-1"


def test_create_invoice_requires_fields(client, auth_headers):
    response = client.post(
        "/v1/invoices", json={"customer_id": "cust_1"}, headers=auth_headers
    )
    assert response.status_code == 400


def test_create_invoice_adds_and_returns_invoice(client, auth_headers):
    response = client.post(
        "/v1/invoices",
        json={"customer_id": "cust_2", "amount_cents": 999},
        headers=auth_headers,
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["customer_id"] == "cust_2"
    assert body["amount_cents"] == 999

    listed = client.get(
        "/v1/invoices", query_string={"customerId": "cust_2"}, headers=auth_headers
    )
    assert len(listed.get_json()["invoices"]) == 1


def test_openapi_document_describes_list_and_create_invoices(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    doc = response.get_json()
    assert doc["paths"]["/v1/invoices"]["get"]["operationId"] == "listInvoices"
    assert doc["paths"]["/v1/invoices"]["post"]["operationId"] == "createInvoice"


def test_list_invoices_without_a_bearer_token_is_401(client):
    response = client.get("/v1/invoices", query_string={"customerId": "cust_1"})
    assert response.status_code == 401


def test_list_invoices_with_a_valid_bearer_token_succeeds(client, monkeypatch, rsa_key):
    from billing_mock import app as app_module

    class _FakeSigningKey:
        key = rsa_key.public_key()

    class _FakeJwksClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    monkeypatch.setattr(app_module, "_jwks_client", _FakeJwksClient())

    token = _bearer(rsa_key)
    response = client.get(
        "/v1/invoices",
        query_string={"customerId": "cust_1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


def test_openapi_document_needs_no_bearer_token(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
