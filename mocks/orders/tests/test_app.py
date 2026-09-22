import pytest

from orders_mock.app import app


_NO_AUTH = object()


class AuthenticatedTestClient:
    def __init__(self, app_test_client, api_key=None):
        self._client = app_test_client
        self._api_key = api_key

    def _add_auth(self, headers):
        if headers is _NO_AUTH:
            return None
        if headers is None:
            headers = {}
        else:
            headers = dict(headers)
        if self._api_key is not None and "X-Api-Key" not in headers:
            headers["X-Api-Key"] = self._api_key
        return headers or None

    def get(self, path, headers=None, **kwargs):
        if headers is _NO_AUTH:
            return self._client.get(path, **kwargs)
        return self._client.get(path, headers=self._add_auth(headers), **kwargs)

    def post(self, path, headers=None, **kwargs):
        if headers is _NO_AUTH:
            return self._client.post(path, **kwargs)
        return self._client.post(path, headers=self._add_auth(headers), **kwargs)


@pytest.fixture
def client():
    app.config.update(TESTING=True)
    return AuthenticatedTestClient(app.test_client(), api_key="orders-mock-test-key")


def test_healthz_returns_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_list_orders_returns_seeded_data(client):
    response = client.get("/v1/orders")
    assert response.status_code == 200
    assert len(response.get_json()["orders"]) == 1


def test_get_order_unknown_id_is_404(client):
    response = client.get("/v1/orders/nope")
    assert response.status_code == 404


def test_get_order_known_id(client):
    response = client.get("/v1/orders/ord_1")
    assert response.status_code == 200
    assert response.get_json()["customer_id"] == "cust_1"


def test_create_order_requires_fields(client):
    response = client.post("/v1/orders", json={"customer_id": "cust_1"})
    assert response.status_code == 400


def test_create_order_adds_and_returns_order(client):
    response = client.post(
        "/v1/orders", json={"customer_id": "cust_4", "item_count": 2}
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["customer_id"] == "cust_4"
    assert body["item_count"] == 2

    listed = client.get("/v1/orders")
    assert len(listed.get_json()["orders"]) == 2


def test_list_orders_without_an_api_key_is_401(client):
    response = client.get("/v1/orders", headers=_NO_AUTH)
    assert response.status_code == 401


def test_list_orders_with_the_wrong_api_key_is_401(client):
    response = client.get("/v1/orders", headers={"X-Api-Key": "wrong"})
    assert response.status_code == 401


def test_list_orders_with_the_right_api_key_succeeds(client):
    response = client.get("/v1/orders", headers={"X-Api-Key": "orders-mock-test-key"})
    assert response.status_code == 200


def test_healthz_needs_no_api_key(client):
    response = client.get("/healthz", headers=_NO_AUTH)
    assert response.status_code == 200
