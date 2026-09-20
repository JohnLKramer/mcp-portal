import pytest

from mcp_portal.classify import UnsupportedMethod, effect_for_method
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
