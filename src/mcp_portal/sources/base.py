"""The source protocol.

A source answers only "what does this upstream offer". It never decides exposure —
that is the registry's job, and keeping the two apart is what stops each mode from
becoming its own introspection code path.
"""

from collections.abc import Iterable
from typing import Protocol

from mcp_portal.operations import Operation


class OperationSource(Protocol):
    def operations(self) -> Iterable[Operation]: ...
