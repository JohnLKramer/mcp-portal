"""The operation domain model. Pure data: no I/O, no imports from other modules."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class Effect(StrEnum):
    """What an operation does to upstream state. Derived from the HTTP method."""

    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    ACTION = "action"


class Sensitivity(StrEnum):
    """Whether an operation's data is safe to expose. Never derived."""

    NORMAL = "normal"
    SENSITIVE = "sensitive"


class ParamLocation(StrEnum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"


class BodyMode(StrEnum):
    FLATTEN = "flatten"
    SINGLE_ARG = "single_arg"


@dataclass(frozen=True, slots=True)
class Parameter:
    """One argument's mapping from tool-schema name to wire representation.

    `arg` is what the model sees; `wire_name` is what the upstream expects. Keeping
    both is what makes schema flattening reversible.
    """

    arg: str
    location: ParamLocation
    wire_name: str
    required: bool
    schema: Mapping[str, Any]
    # Narrow on purpose: the HTTP transport only serializes `form`, so a wider
    # type here would promise an interpretation no code performs.
    style: Literal["form"] = "form"
    explode: bool = True


@dataclass(frozen=True, slots=True)
class BodySpec:
    content_type: str
    schema: Mapping[str, Any]
    mode: BodyMode = BodyMode.FLATTEN


@dataclass(frozen=True, slots=True)
class HttpBinding:
    method: str
    path: str
    parameters: tuple[Parameter, ...] = ()
    body: BodySpec | None = None
    protocol: Literal["http"] = "http"


# A closed union, discriminated on `protocol`. GrpcBinding and GraphQlBinding join
# it in later slices; nothing outside transports/ may inspect a binding's internals.
type Binding = HttpBinding


@dataclass(frozen=True, slots=True)
class Operation:
    """One callable upstream operation, independent of how it was discovered."""

    id: str
    upstream: str
    name: str
    title: str
    description: str
    group_tags: tuple[str, ...]
    effect: Effect
    sensitivity: Sensitivity
    input_schema: Mapping[str, Any]
    binding: Binding
    meta: Mapping[str, Any] = field(default_factory=dict)
