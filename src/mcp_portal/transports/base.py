"""The transport adapter protocol.

A transport receives a ready credential and never reasons about OAuth. That seam
is why later protocol slices inherit the auth broker unchanged.
"""

from typing import Any, Protocol

from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.operations import Operation


class ToolCallResult(Protocol):
    @property
    def status(self) -> int: ...
    @property
    def text(self) -> str: ...
    @property
    def truncated(self) -> bool: ...


class TransportAdapter(Protocol):
    async def execute(
        self,
        operation: Operation,
        arguments: dict[str, Any],
        carry: tuple[AuthorizationDetail, ...] = (),
        subject_token: str | None = None,
        subject_token_expires_at: int | None = None,
    ) -> ToolCallResult: ...
