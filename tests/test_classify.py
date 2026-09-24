import pytest

from mcp_portal.classify import (
    UnsupportedMethod,
    UnsupportedOperationType,
    effect_for_method,
    effect_for_operation_type,
)
from mcp_portal.operations import Effect


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("GET", Effect.READ_ONLY),
        ("PUT", Effect.IDEMPOTENT_WRITE),
        ("DELETE", Effect.IDEMPOTENT_WRITE),
        ("POST", Effect.ACTION),
        ("PATCH", Effect.ACTION),
    ],
)
def test_effect_derives_from_http_method(method: str, expected: Effect):
    assert effect_for_method(method) is expected


def test_method_matching_is_case_insensitive():
    assert effect_for_method("get") is Effect.READ_ONLY


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "TRACE"])
def test_methods_that_are_never_exposed_raise(method: str):
    with pytest.raises(UnsupportedMethod):
        effect_for_method(method)


@pytest.mark.parametrize(
    ("operation_type", "expected"),
    [("query", Effect.READ_ONLY), ("mutation", Effect.ACTION)],
)
def test_effect_derives_from_graphql_operation_type(operation_type: str, expected: Effect):
    assert effect_for_operation_type(operation_type) is expected


def test_subscription_is_never_exposed_as_a_tool():
    with pytest.raises(UnsupportedOperationType):
        effect_for_operation_type("subscription")
