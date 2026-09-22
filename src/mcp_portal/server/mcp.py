"""Convert Operations to MCP tools and execute tool calls.

Error categories are chosen for what a model should *do* about them: a validation
failure names the field to fix, an upstream 4xx surfaces the upstream's own
message, and neither is retried by the client on its own initiative.
"""

from collections.abc import Mapping
from typing import Any

import httpx
import jsonschema
from mcp import types

from mcp_portal.auth.principal import Principal
from mcp_portal.operations import Effect, Operation
from mcp_portal.policy import PolicyEngine
from mcp_portal.registry import ToolSet
from mcp_portal.transports.http import HttpTransport, RequestBuildError

_ANNOTATIONS: dict[Effect, tuple[bool, bool, bool]] = {
    # effect: (read_only, destructive, idempotent)
    Effect.READ_ONLY: (True, False, True),
    Effect.IDEMPOTENT_WRITE: (False, True, True),
    Effect.ACTION: (False, True, False),
}


def annotations_for(operation: Operation) -> types.ToolAnnotations:
    read_only, destructive, idempotent = _ANNOTATIONS[operation.effect]
    return types.ToolAnnotations(
        title=operation.title,
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        # Always true: the gateway calls an external system whose state it does not control.
        open_world_hint=True,
    )


def to_mcp_tool(operation: Operation) -> types.Tool:
    return types.Tool(
        name=operation.name,
        title=operation.title,
        description=operation.description,
        input_schema=dict(operation.input_schema),
        annotations=annotations_for(operation),
    )


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


class ToolInvoker:
    def __init__(
        self,
        toolset: ToolSet,
        transports: Mapping[str, HttpTransport],
        policy: PolicyEngine,
        principal: Principal | None = None,
    ) -> None:
        self._toolset = toolset
        self._transports = transports
        self._policy = policy
        self._principal = principal if principal is not None else Principal("local", ())

    def tools(self) -> list[types.Tool]:
        return [to_mcp_tool(op) for op in self._toolset.operations]

    async def call(self, name: str, arguments: Mapping[str, Any] | None) -> types.CallToolResult:
        operation = self._toolset.by_name.get(name)
        if operation is None:
            return _error(f"unknown tool {name!r}")

        args = dict(arguments or {})
        try:
            jsonschema.validate(args, dict(operation.input_schema))
        except jsonschema.ValidationError as exc:
            field = ".".join(str(p) for p in exc.absolute_path)
            where = f" at {field}" if field else ""
            return _error(f"invalid arguments for {name!r}{where}: {exc.message}")

        decision = self._policy.evaluate(operation, self._principal)
        if not decision.allowed:
            missing = ", ".join(d.type for d in decision.missing) or "policy default is deny"
            # Never the token/principal contents (§10) — only which
            # requirement type was missing.
            return _error(f"authorization denied for {name!r}: missing {missing}")

        transport = self._transports.get(operation.upstream)
        if transport is None:
            return _error(f"no transport configured for upstream {operation.upstream!r}")

        try:
            response = await transport.execute(operation, args)
        except RequestBuildError as exc:
            return _error(f"invalid arguments for {name!r}: {exc}")
        except httpx.TimeoutException:
            return _error(f"upstream {operation.upstream!r} timed out")
        except httpx.RequestError as exc:
            # The supertype of every transport-level httpx failure: connect
            # errors, protocol errors, redirect loops. None of them should reach
            # the client as a traceback instead of a tool result.
            return _error(f"upstream {operation.upstream!r} request failed: {exc}")

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=response.text)],
            is_error=not (200 <= response.status < 300),
        )
