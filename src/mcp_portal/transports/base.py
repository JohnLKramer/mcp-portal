"""The transport adapter protocol.

A transport receives a ready credential and never reasons about OAuth. That seam
is why later protocol slices inherit the auth broker unchanged.
"""

from typing import Protocol

from mcp_portal.operations import Operation


class ToolCallResult(Protocol):
    status: int
    text: str
    truncated: bool


class TransportAdapter(Protocol):
    async def execute(
        self, operation: Operation, arguments: dict[str, object]
    ) -> ToolCallResult: ...
