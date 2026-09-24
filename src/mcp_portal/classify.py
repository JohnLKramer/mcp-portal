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


_OPERATION_TYPE_EFFECT: dict[str, Effect] = {
    "query": Effect.READ_ONLY,
    "mutation": Effect.ACTION,
}

EXPOSED_GRAPHQL_OPERATION_TYPES = frozenset(_OPERATION_TYPE_EFFECT)


class UnsupportedOperationType(Exception):
    """Raised for a GraphQL operation type that is never exposed as a tool."""


def effect_for_operation_type(operation_type: str) -> Effect:
    """Map a GraphQL operation type to an Effect.

    GraphQL has no verb semantics to key an idempotent-write tier on the way
    HTTP's PUT/DELETE do, so a mutation is always `action` regardless of
    whether it happens to be idempotent upstream. `subscription` raises: no
    MCP tool has a streaming return channel yet.
    """
    try:
        return _OPERATION_TYPE_EFFECT[operation_type]
    except KeyError:
        raise UnsupportedOperationType(
            f"GraphQL operation type {operation_type!r} is never exposed as a tool; "
            f"exposed types are {sorted(EXPOSED_GRAPHQL_OPERATION_TYPES)}"
        ) from None
