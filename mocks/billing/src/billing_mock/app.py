"""In-memory mock of the billing API described by examples/billing.yaml."""

import os

import jwt
from flask import Flask, jsonify, request

app = Flask(__name__)

_OAUTH_ISSUER = os.environ.get("MOCK_OAUTH2_ISSUER", "http://mock-oauth2-server:8080/default")
_jwks_client = jwt.PyJWKClient(f"{_OAUTH_ISSUER}/jwks")


@app.before_request
def _require_bearer_token():
    if request.path in ("/healthz", "/openapi.json"):
        return None
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify(error="missing bearer token"), 401
    token = auth_header.removeprefix("Bearer ")
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience="billing-mock",
            issuer=_OAUTH_ISSUER,
        )
    except (jwt.InvalidTokenError, jwt.PyJWKClientError):
        return jsonify(error="invalid bearer token"), 401
    return None


_INVOICES: dict[str, list[dict[str, object]]] = {
    "cust_1": [
        {"id": "inv_1", "customer_id": "cust_1", "amount_cents": 4200},
        {"id": "inv_2", "customer_id": "cust_1", "amount_cents": 1500},
    ],
}
_TAX_IDS = {"cust_1": "TAX-CUST-1"}
_next_invoice_id = 3

_OPENAPI_DOC = {
    "openapi": "3.0.3",
    "info": {"title": "Billing Mock API", "version": "1.0.0"},
    "servers": [{"url": "http://localhost:8080"}],
    "paths": {
        "/v1/invoices": {
            "get": {
                "operationId": "listInvoices",
                "parameters": [
                    {
                        "name": "customerId",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {"200": {"description": "OK"}},
            },
            "post": {
                "operationId": "createInvoice",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "customer_id": {"type": "string"},
                                    "amount_cents": {"type": "integer"},
                                },
                                "required": ["customer_id", "amount_cents"],
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
    },
}


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/v1/invoices")
def list_invoices():
    customer_id = request.args.get("customerId")
    if not customer_id:
        return jsonify(error="customerId is required"), 400
    invoices = _INVOICES.get(customer_id, [])
    limit = request.args.get("limit", type=int)
    if limit is not None:
        invoices = invoices[:limit]
    return jsonify(invoices=invoices)


@app.get("/v1/customers/<customer_id>/tax-id")
def get_tax_id(customer_id: str):
    tax_id = _TAX_IDS.get(customer_id)
    if tax_id is None:
        return jsonify(error="unknown customer"), 404
    return jsonify(customer_id=customer_id, tax_id=tax_id)


@app.post("/v1/invoices")
def create_invoice():
    global _next_invoice_id
    body = request.get_json(force=True, silent=True) or {}
    customer_id = body.get("customer_id")
    amount_cents = body.get("amount_cents")
    if not customer_id or not isinstance(amount_cents, int):
        return jsonify(error="customer_id and amount_cents are required"), 400
    invoice = {
        "id": f"inv_{_next_invoice_id}",
        "customer_id": customer_id,
        "amount_cents": amount_cents,
    }
    _next_invoice_id += 1
    _INVOICES.setdefault(customer_id, []).append(invoice)
    return jsonify(invoice), 201


@app.get("/openapi.json")
def openapi_document():
    return jsonify(_OPENAPI_DOC)
