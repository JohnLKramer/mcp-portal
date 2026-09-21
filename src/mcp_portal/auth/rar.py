"""RFC 9396 Rich Authorization Requests: the coverage predicate and claim parsing.

This module never learns where an `authorization_details` set came from — a
JWT claim, a local config list — so it is shared unchanged between the local
principal (P3) and the inbound JWT principal (P4).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


class RarError(Exception):
    """Raised when an `authorization_details` value is malformed.

    Per §8 of the design, a malformed claim must deny the request rather than
    be treated as absent — callers translate this into a denial or, for a
    config-sourced principal, a startup `ConfigError`.
    """


@dataclass(frozen=True, slots=True)
class AuthorizationDetail:
    """One RFC 9396 authorization detail, either required or presented."""

    type: str
    actions: tuple[str, ...] = field(default=())
    locations: tuple[str, ...] = field(default=())
    datatypes: tuple[str, ...] = field(default=())
    identifier: str | None = None
    privileges: tuple[str, ...] = field(default=())


def covers(required: AuthorizationDetail, presented: Iterable[AuthorizationDetail]) -> bool:
    """Is `required` satisfied by a single detail in `presented`? (§8)

    Rules apply only to fields `required` actually specifies. Coverage must
    come from one presented detail — composing two narrow grants into an
    authority neither conveyed is deliberately not supported.
    """
    return any(_covers_one(required, p) for p in presented)


def _covers_one(required: AuthorizationDetail, presented: AuthorizationDetail) -> bool:
    if presented.type != required.type:
        return False
    if required.actions and not set(required.actions) <= set(presented.actions):
        return False
    if required.locations and not set(required.locations) <= set(presented.locations):
        return False
    if required.datatypes and not set(required.datatypes) <= set(presented.datatypes):
        return False
    if required.privileges and not set(required.privileges) <= set(presented.privileges):
        return False
    return not (required.identifier is not None and presented.identifier != required.identifier)


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RarError(f"authorization detail field {field_name!r} must be a list of strings")
    return tuple(value)


def parse_authorization_details(raw: object) -> tuple[AuthorizationDetail, ...]:
    """Parse a raw `authorization_details` value into domain objects.

    Presented details (unlike `require`) may carry type-specific extension
    fields per RFC 9396; only the fields sidekit's coverage predicate reads
    are extracted, and unrecognized fields are ignored rather than rejected —
    strict rejection is reserved for `require`, the one place policy must fail
    closed (§5).
    """
    if not isinstance(raw, list):
        raise RarError(f"authorization_details must be a list, got {type(raw).__name__}")

    parsed: list[AuthorizationDetail] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise RarError(
                f"authorization detail entries must be objects, got {type(entry).__name__}"
            )
        detail_type = entry.get("type")
        if not isinstance(detail_type, str):
            raise RarError("authorization detail is missing a string 'type' field")
        identifier = entry.get("identifier")
        if identifier is not None and not isinstance(identifier, str):
            raise RarError("authorization detail field 'identifier' must be a string")
        parsed.append(
            AuthorizationDetail(
                type=detail_type,
                actions=_string_tuple(entry.get("actions", []), "actions"),
                locations=_string_tuple(entry.get("locations", []), "locations"),
                datatypes=_string_tuple(entry.get("datatypes", []), "datatypes"),
                identifier=identifier,
                privileges=_string_tuple(entry.get("privileges", []), "privileges"),
            )
        )
    return tuple(parsed)
