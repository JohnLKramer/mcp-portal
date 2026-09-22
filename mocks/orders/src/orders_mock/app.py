"""In-memory mock of a second, distinct API used to exercise multi-upstream configs."""

from flask import Flask, jsonify, request

app = Flask(__name__)

_API_KEY = "orders-mock-test-key"


@app.before_request
def _require_api_key():
    if request.path == "/healthz":
        return None
    if request.headers.get("X-Api-Key") != _API_KEY:
        return jsonify(error="missing or invalid API key"), 401
    return None

_ORDERS: dict[str, dict[str, object]] = {
    "ord_1": {"id": "ord_1", "customer_id": "cust_1", "item_count": 3},
}
_next_order_id = 2


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/v1/orders")
def list_orders():
    return jsonify(orders=list(_ORDERS.values()))


@app.get("/v1/orders/<order_id>")
def get_order(order_id: str):
    order = _ORDERS.get(order_id)
    if order is None:
        return jsonify(error="unknown order"), 404
    return jsonify(order)


@app.post("/v1/orders")
def create_order():
    global _next_order_id
    body = request.get_json(force=True, silent=True) or {}
    customer_id = body.get("customer_id")
    item_count = body.get("item_count")
    if not customer_id or not isinstance(item_count, int):
        return jsonify(error="customer_id and item_count are required"), 400
    order_id = f"ord_{_next_order_id}"
    _next_order_id += 1
    order = {"id": order_id, "customer_id": customer_id, "item_count": item_count}
    _ORDERS[order_id] = order
    return jsonify(order), 201
