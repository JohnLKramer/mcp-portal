from mcp_portal.sources.openapi_paths import default_operation_id, extract_raw_operations

DOC: dict = {
    "openapi": "3.1.0",
    "paths": {
        "/invoices": {
            "parameters": [
                {
                    "name": "customerId",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ],
            "get": {"operationId": "list_invoices", "tags": ["billing"], "responses": {}},
            "post": {
                "operationId": "create_invoice",
                "tags": ["billing"],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
                "responses": {},
            },
        },
        "/invoices/{id}": {
            "get": {
                "operationId": "get_invoice",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {},
            },
            "delete": {"operationId": "cancel_invoice", "x-mcp-sensitive": True, "responses": {}},
        },
    },
}


def test_default_operation_id_slugs_method_and_path():
    assert default_operation_id("GET", "/v1/invoices/{id}") == "get_v1_invoices_id"


def test_every_exposed_method_on_every_path_is_extracted():
    ops = extract_raw_operations(DOC)
    assert {(o.method, o.path) for o in ops} == {
        ("get", "/invoices"),
        ("post", "/invoices"),
        ("get", "/invoices/{id}"),
        ("delete", "/invoices/{id}"),
    }


def test_head_and_options_are_never_extracted():
    doc = {
        "paths": {
            "/x": {
                "head": {"operationId": "probe", "responses": {}},
                "options": {"operationId": "cors", "responses": {}},
            }
        }
    }
    assert extract_raw_operations(doc) == []


def test_path_level_parameters_are_inherited():
    ops = {o.operation_id: o for o in extract_raw_operations(DOC)}
    names = {p.name for p in ops["list_invoices"].parameters}
    assert "customerId" in names


def test_operation_level_parameters_win_on_name_and_location_collision():
    doc = {
        "paths": {
            "/x/{id}": {
                "parameters": [
                    {"name": "id", "in": "path", "required": False, "schema": {"type": "integer"}}
                ],
                "get": {
                    "operationId": "get_x",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {},
                },
            }
        }
    }
    (op,) = extract_raw_operations(doc)
    (param,) = op.parameters
    assert param.required is True
    assert param.schema == {"type": "string"}


def test_cookie_parameters_are_dropped():
    doc = {
        "paths": {
            "/x": {
                "get": {
                    "operationId": "get_x",
                    "parameters": [
                        {"name": "session", "in": "cookie", "schema": {"type": "string"}}
                    ],
                    "responses": {},
                }
            }
        }
    }
    (op,) = extract_raw_operations(doc)
    assert op.parameters == ()


def test_request_body_prefers_application_json_when_present():
    ops = {o.operation_id: o for o in extract_raw_operations(DOC)}
    body = ops["create_invoice"].request_body
    assert body is not None
    assert body["content_type"] == "application/json"


def test_an_operation_with_no_request_body_has_none():
    ops = {o.operation_id: o for o in extract_raw_operations(DOC)}
    assert ops["list_invoices"].request_body is None


def test_a_non_json_only_request_body_carries_its_actual_content_type():
    doc = {
        "paths": {
            "/x": {
                "post": {
                    "operationId": "upload",
                    "requestBody": {
                        "content": {"multipart/form-data": {"schema": {"type": "object"}}}
                    },
                    "responses": {},
                }
            }
        }
    }
    (op,) = extract_raw_operations(doc)
    assert op.request_body["content_type"] == "multipart/form-data"


def test_x_mcp_extensions_are_captured():
    ops = {o.operation_id: o for o in extract_raw_operations(DOC)}
    assert ops["cancel_invoice"].extensions == {"x-mcp-sensitive": True}


def test_deprecated_operations_are_excluded_by_default():
    doc = {"paths": {"/x": {"get": {"operationId": "old", "deprecated": True, "responses": {}}}}}
    assert extract_raw_operations(doc) == []


def test_deprecated_operations_are_included_when_asked():
    doc = {"paths": {"/x": {"get": {"operationId": "old", "deprecated": True, "responses": {}}}}}
    (op,) = extract_raw_operations(doc, include_deprecated=True)
    assert op.operation_id == "old"


def test_tags_summary_and_description_are_carried_through():
    doc = {
        "paths": {
            "/x": {
                "get": {
                    "operationId": "get_x",
                    "tags": ["billing", "admin"],
                    "summary": "Get X.",
                    "description": "Longer description.",
                    "responses": {},
                }
            }
        }
    }
    (op,) = extract_raw_operations(doc)
    assert op.tags == ("billing", "admin")
    assert op.summary == "Get X."
    assert op.description == "Longer description."


def test_operations_with_no_operation_id_have_none():
    doc = {"paths": {"/x": {"get": {"responses": {}}}}}
    (op,) = extract_raw_operations(doc)
    assert op.operation_id is None
