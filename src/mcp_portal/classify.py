"""Derive an operation's effect from its HTTP method.

RFC 9110 already defines safe and idempotent method semantics, so the correct
default is available for free and needs no hand-authoring.
"""

from mcp_portal.operations import Effect

_METHOD_EFFECT: dict[str, Effect] = {
    "GET": Effect.READ_ONLY,
    "PUT": Effect.IDEMPOTENT_WRITE,
    "DELETE": Effect.IDEMPOTENT_WRITE,
    "POST": Effect.ACTION,
    "PATCH": Effect.ACTION,
}

EXPOSED_METHODS = frozenset(_METHOD_EFFECT)


class UnsupportedMethod(Exception):
    """Raised for a method that is never exposed as a tool."""


def effect_for_method(method: str) -> Effect:
    """Map an HTTP method to an Effect.

    HEAD and OPTIONS raise: they carry no response body a model can use and exist
    for cache and CORS negotiation, so they are dropped at the source in every mode.
    """
    try:
        return _METHOD_EFFECT[method.upper()]
    except KeyError:
        raise UnsupportedMethod(
            f"HTTP method {method!r} is never exposed as a tool; "
            f"exposed methods are {sorted(EXPOSED_METHODS)}"
        ) from None
