import pytest

from orders_mock.app import app


@pytest.fixture
def client():
    app.config.update(TESTING=True)
    return app.test_client()


def test_healthz_returns_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_list_orders_returns_seeded_data(client):
    response = client.get("/v1/orders", headers={"X-Api-Key": "orders-mock-test-key"})
    assert response.status_code == 200
    assert len(response.get_json()["orders"]) == 1


def test_get_order_unknown_id_is_404(client):
    response = client.get("/v1/orders/nope", headers={"X-Api-Key": "orders-mock-test-key"})
    assert response.status_code == 404


def test_get_order_known_id(client):
    response = client.get("/v1/orders/ord_1", headers={"X-Api-Key": "orders-mock-test-key"})
    assert response.status_code == 200
    assert response.get_json()["customer_id"] == "cust_1"


def test_create_order_requires_fields(client):
    response = client.post("/v1/orders", json={"customer_id": "cust_1"}, headers={"X-Api-Key": "orders-mock-test-key"})
    assert response.status_code == 400


def test_create_order_adds_and_returns_order(client):
    response = client.post(
        "/v1/orders", json={"customer_id": "cust_4", "item_count": 2}, headers={"X-Api-Key": "orders-mock-test-key"}
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["customer_id"] == "cust_4"
    assert body["item_count"] == 2

    listed = client.get("/v1/orders", headers={"X-Api-Key": "orders-mock-test-key"})
    assert len(listed.get_json()["orders"]) == 2


def test_list_orders_without_an_api_key_is_401(client):
    response = client.get("/v1/orders")
    assert response.status_code == 401


def test_list_orders_with_the_wrong_api_key_is_401(client):
    response = client.get("/v1/orders", headers={"X-Api-Key": "wrong"})
    assert response.status_code == 401


def test_list_orders_with_the_right_api_key_succeeds(client):
    response = client.get("/v1/orders", headers={"X-Api-Key": "orders-mock-test-key"})
    assert response.status_code == 200


def test_healthz_needs_no_api_key(client):
    response = client.get("/healthz")
    assert response.status_code == 200
