import pytest

from billing_mock.app import app


@pytest.fixture
def client():
    app.config.update(TESTING=True)
    return app.test_client()


def test_healthz_returns_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_list_invoices_requires_customer_id(client):
    response = client.get("/v1/invoices")
    assert response.status_code == 400


def test_list_invoices_returns_seeded_data(client):
    response = client.get("/v1/invoices", query_string={"customerId": "cust_1"})
    assert response.status_code == 200
    assert len(response.get_json()["invoices"]) == 2


def test_list_invoices_respects_limit(client):
    response = client.get(
        "/v1/invoices", query_string={"customerId": "cust_1", "limit": 1}
    )
    assert len(response.get_json()["invoices"]) == 1


def test_get_tax_id_unknown_customer_is_404(client):
    response = client.get("/v1/customers/nope/tax-id")
    assert response.status_code == 404


def test_get_tax_id_known_customer(client):
    response = client.get("/v1/customers/cust_1/tax-id")
    assert response.status_code == 200
    assert response.get_json()["tax_id"] == "TAX-CUST-1"


def test_create_invoice_requires_fields(client):
    response = client.post("/v1/invoices", json={"customer_id": "cust_1"})
    assert response.status_code == 400


def test_create_invoice_adds_and_returns_invoice(client):
    response = client.post(
        "/v1/invoices", json={"customer_id": "cust_2", "amount_cents": 999}
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["customer_id"] == "cust_2"
    assert body["amount_cents"] == 999

    listed = client.get("/v1/invoices", query_string={"customerId": "cust_2"})
    assert len(listed.get_json()["invoices"]) == 1
