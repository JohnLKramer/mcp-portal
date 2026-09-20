"""Turn config `operations[]` entries into Operations.

An explicit entry and an introspected operation produce the identical model; an
entry simply hand-writes what OpenAPI would have supplied. `input_schema` is
always synthesized from the binding, never hand-written, so both sources present
identical tool schemas for identical bindings.
"""

from collections.abc import Iterable

from mcp_portal.classify import UnsupportedMethod, effect_for_method
from mcp_portal.config.loader import ConfigError
from mcp_portal.config.models import BindingEntry, Config, OperationEntry
from mcp_portal.operations import (
    BodySpec,
    HttpBinding,
    Operation,
    Parameter,
    Sensitivity,
)
from mcp_portal.sources.flatten import FlattenError, build_input_schema, resolve_arg_names


def _binding(entry: BindingEntry) -> HttpBinding:
    return HttpBinding(
        method=entry.method.upper(),
        path=entry.path,
        parameters=tuple(
            Parameter(
                arg=p.arg,
                location=p.location,
                wire_name=p.wire_name or p.arg,
                required=p.required,
                schema=p.schema_,
                style=p.style,
                explode=p.explode,
            )
            for p in entry.parameters
        ),
        body=(
            BodySpec(
                content_type=entry.body.content_type,
                schema=entry.body.schema_,
                mode=entry.body.mode,
            )
            if entry.body
            else None
        ),
    )


class ExplicitSource:
    def __init__(self, config: Config) -> None:
        self._config = config

    def operations(self) -> Iterable[Operation]:
        for entry in self._config.operations:
            op = self._build(entry)
            if op is not None:
                yield op

    def _build(self, entry: OperationEntry) -> Operation | None:
        binding = _binding(entry.binding)

        try:
            derived_effect = effect_for_method(binding.method)
        except UnsupportedMethod:
            # HEAD and OPTIONS (and any other unsupported method) are dropped at
            # the source, in every mode — even when config sets an explicit effect.
            return None
        effect = entry.effect or derived_effect

        try:
            resolved = resolve_arg_names(binding)
            input_schema = build_input_schema(binding)
        except FlattenError as exc:
            raise ConfigError(f"operation {entry.id!r}: {exc}") from exc

        return Operation(
            id=entry.id,
            upstream=entry.upstream,
            name=entry.name or "",
            title=entry.title or next(iter(entry.description.splitlines()), entry.id),
            description=entry.description,
            group_tags=tuple(entry.group_tags),
            effect=effect,
            sensitivity=entry.sensitivity or Sensitivity.NORMAL,
            input_schema=input_schema,
            binding=resolved,
        )
