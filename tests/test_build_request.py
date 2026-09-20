import json

import pytest

from mcp_portal.operations import BodyMode, BodySpec, HttpBinding, Parameter, ParamLocation
from mcp_portal.transports.http import Credential, RequestBuildError, build_request

BASE = "https://api.example.com"


def p(arg: str, location: ParamLocation, wire: str | None = None, required: bool = True, **kw):
    return Parameter(
        arg=arg,
        location=location,
        wire_name=wire or arg,
        required=required,
        schema={"type": "string"},
        **kw,
    )


def test_path_parameters_are_substituted():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices/{id}",
        parameters=(p("invoice_id", ParamLocation.PATH, wire="id"),),
    )
    req = build_request(binding, BASE, {"invoice_id": "inv_1"}, None)
    assert req.url == "https://api.example.com/v1/invoices/inv_1"
    assert req.method == "GET"


@pytest.mark.parametrize(
    "value",
    ["../admin/reset", "..%2Fadmin", "a/b", "a?b", "a#b", "%2e%2e%2fadmin", "http://evil/x"],
)
def test_path_traversal_and_separators_cannot_escape_the_segment(value: str):
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices/{id}",
        parameters=(p("invoice_id", ParamLocation.PATH, wire="id"),),
    )
    req = build_request(binding, BASE, {"invoice_id": value}, None)
    tail = req.url.removeprefix("https://api.example.com/v1/invoices/")
    assert "/" not in tail
    assert "?" not in tail
    assert "#" not in tail
    assert req.url.startswith("https://api.example.com/v1/invoices/")


def test_empty_required_path_value_is_an_error():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices/{id}",
        parameters=(p("invoice_id", ParamLocation.PATH, wire="id"),),
    )
    with pytest.raises(RequestBuildError, match="invoice_id"):
        build_request(binding, BASE, {"invoice_id": ""}, None)


def test_query_parameters_are_encoded_and_use_wire_names():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices",
        parameters=(p("customer_id", ParamLocation.QUERY, wire="customerId"),),
    )
    req = build_request(binding, BASE, {"customer_id": "a b&c"}, None)
    assert req.url == "https://api.example.com/v1/invoices?customerId=a+b%26c"


def test_exploded_array_query_repeats_the_key():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices",
        parameters=(p("status", ParamLocation.QUERY, explode=True),),
    )
    req = build_request(binding, BASE, {"status": ["open", "paid"]}, None)
    assert req.url.endswith("?status=open&status=paid")


def test_non_exploded_array_query_is_comma_joined():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices",
        parameters=(p("status", ParamLocation.QUERY, explode=False),),
    )
    req = build_request(binding, BASE, {"status": ["open", "paid"]}, None)
    assert req.url.endswith("?status=open%2Cpaid")


def test_omitted_optional_parameters_are_dropped():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices",
        parameters=(p("cursor", ParamLocation.QUERY, required=False),),
    )
    assert build_request(binding, BASE, {}, None).url == "https://api.example.com/v1/invoices"


def test_missing_required_parameter_is_an_error():
    binding = HttpBinding(
        method="GET",
        path="/v1/invoices",
        parameters=(p("customer_id", ParamLocation.QUERY),),
    )
    with pytest.raises(RequestBuildError):
        build_request(binding, BASE, {}, None)


def test_header_parameters_are_sent():
    binding = HttpBinding(
        method="GET",
        path="/v1/x",
        parameters=(p("trace", ParamLocation.HEADER, wire="X-Trace-Id"),),
    )
    req = build_request(binding, BASE, {"trace": "abc"}, None)
    assert req.headers["X-Trace-Id"] == "abc"


@pytest.mark.parametrize("value", ["a\r\nX-Evil: 1", "a\nX-Evil: 1", "a\rX-Evil: 1"])
def test_crlf_in_a_header_value_is_rejected(value: str):
    binding = HttpBinding(
        method="GET",
        path="/v1/x",
        parameters=(p("trace", ParamLocation.HEADER, wire="X-Trace-Id"),),
    )
    with pytest.raises(RequestBuildError):
        build_request(binding, BASE, {"trace": value}, None)


def test_flattened_body_is_reassembled_with_wire_names():
    binding = HttpBinding(
        method="POST",
        path="/v1/invoices",
        body=BodySpec(
            content_type="application/json",
            schema={"type": "object", "properties": {"amount": {"type": "integer"}}},
        ),
    )
    req = build_request(binding, BASE, {"amount": 100}, None)
    assert json.loads(req.body or b"") == {"amount": 100}
    assert req.headers["Content-Type"] == "application/json"


def test_single_arg_body_is_sent_whole():
    binding = HttpBinding(
        method="POST",
        path="/v1/bulk",
        body=BodySpec(
            content_type="application/json", schema={"type": "array"}, mode=BodyMode.SINGLE_ARG
        ),
    )
    req = build_request(binding, BASE, {"body": [1, 2]}, None)
    assert json.loads(req.body or b"") == [1, 2]


def test_missing_required_single_arg_body_is_an_error():
    binding = HttpBinding(
        method="POST",
        path="/v1/bulk",
        body=BodySpec(
            content_type="application/json", schema={"type": "array"}, mode=BodyMode.SINGLE_ARG
        ),
    )
    with pytest.raises(RequestBuildError):
        build_request(binding, BASE, {}, None)


def test_missing_required_non_object_body_is_an_error():
    binding = HttpBinding(
        method="POST",
        path="/v1/bulk",
        body=BodySpec(content_type="application/json", schema={"type": "array"}),
    )
    with pytest.raises(RequestBuildError):
        build_request(binding, BASE, {}, None)


def test_missing_required_flatten_body_property_is_an_error():
    binding = HttpBinding(
        method="POST",
        path="/v1/invoices",
        body=BodySpec(
            content_type="application/json",
            schema={
                "type": "object",
                "properties": {"amount": {"type": "integer"}, "currency": {"type": "string"}},
                "required": ["amount", "currency"],
            },
        ),
    )
    with pytest.raises(RequestBuildError):
        build_request(binding, BASE, {"amount": 100}, None)


def test_credential_is_attached():
    binding = HttpBinding(method="GET", path="/v1/x")
    req = build_request(binding, BASE, {}, Credential(header="Authorization", value="Bearer t"))
    assert req.headers["Authorization"] == "Bearer t"


def test_credential_is_attached_last_and_wins():
    binding = HttpBinding(
        method="GET",
        path="/v1/x",
        parameters=(p("auth", ParamLocation.HEADER, wire="X-Api-Key", required=False),),
    )
    req = build_request(
        binding, BASE, {"auth": "model-supplied"}, Credential(header="X-Api-Key", value="real")
    )
    assert req.headers["X-Api-Key"] == "real"


def test_base_url_trailing_slash_does_not_double_the_separator():
    binding = HttpBinding(method="GET", path="/v1/x")
    assert build_request(binding, BASE + "/", {}, None).url == "https://api.example.com/v1/x"


def test_base_url_path_prefix_is_preserved():
    binding = HttpBinding(method="GET", path="/invoices")
    req = build_request(binding, "https://api.example.com/v1", {}, None)
    assert req.url == "https://api.example.com/v1/invoices"
