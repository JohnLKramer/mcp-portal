"""Build and execute HTTP requests for an HttpBinding.

`build_request` is pure and is the highest-risk code in the system: it is the
boundary where model-supplied values become a real request.
"""

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

import httpx

from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.config.models import UpstreamConfig
from mcp_portal.operations import BodyMode, Effect, HttpBinding, Operation, ParamLocation
from mcp_portal.transports.common import (
    MAX_ATTEMPTS,
    is_retryable_status,
    map_body,
    wait_for_retry,
)

_CRLF = ("\r", "\n")


class RequestBuildError(Exception):
    """Raised when validated arguments still cannot form a safe request."""


class CredentialSource(Protocol):
    """Produces the credential to attach to one call.

    `carry` is the accumulated set of RAR details the matching policy rules
    said to carry (§8's `outbound.carry`) — empty when no rule asked for it.
    `subject_token` is the raw inbound bearer token, present only under
    `transport: http` with inbound auth enabled; every mode but
    `token_exchange` ignores it. `subject_token_expires_at` is that token's
    expiry (epoch seconds), used only by `token_exchange` to cap the
    exchanged token's TTL.
    """

    async def get(
        self,
        carry: tuple[AuthorizationDetail, ...],
        subject_token: str | None = None,
        subject_token_expires_at: int | None = None,
    ) -> "Credential | None": ...  # noqa: UP037 - Credential is defined below this class


@dataclass(frozen=True, slots=True)
class Credential:
    header: str
    value: str


@dataclass(frozen=True, slots=True)
class StaticCredentialSource:
    """Wraps a `none`/`static` credential resolved once at startup.

    Neither mode varies per call, so `carry` and `subject_token` are accepted
    (to satisfy `CredentialSource`) and ignored.
    """

    credential: Credential | None

    async def get(
        self,
        carry: tuple[AuthorizationDetail, ...],
        subject_token: str | None = None,
        subject_token_expires_at: int | None = None,
    ) -> Credential | None:
        return self.credential


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


def _encode_path_value(arg: str, value: object) -> str:
    """Percent-encode against the RFC 3986 unreserved set.

    `safe=""` is the entire defence: it encodes `/`, `?`, `#` and `%`, so a value
    like `../admin/reset` cannot escape its path segment and reach an operation
    the registry never exposed.
    """
    text = str(value)
    if text == "":
        raise RequestBuildError(
            f"empty value for path argument {arg!r} would collapse the segment and change the route"
        )
    return quote(text, safe="")


def _query_pairs(arg: str, value: object, explode: bool) -> list[tuple[str, str]]:
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
        if explode:
            return [(arg, v) for v in items]
        return [(arg, ",".join(items))]
    if isinstance(value, bool):
        return [(arg, "true" if value else "false")]
    return [(arg, str(value))]


def _check_header_value(name: str, value: str) -> str:
    if any(c in value for c in _CRLF):
        raise RequestBuildError(
            f"header {name!r} value contains CR or LF, which would allow header injection"
        )
    return value


def _join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def build_request(
    binding: HttpBinding,
    base_url: str,
    arguments: Mapping[str, Any],
    credential: Credential | None,
) -> PreparedRequest:
    path = binding.path
    query: list[tuple[str, str]] = []
    headers: dict[str, str] = {}
    body_fields: dict[str, Any] = {}

    for param in binding.parameters:
        if param.arg not in arguments:
            if param.required:
                raise RequestBuildError(f"missing required argument {param.arg!r}")
            continue
        value = arguments[param.arg]

        match param.location:
            case ParamLocation.PATH:
                path = path.replace(
                    "{" + param.wire_name + "}", _encode_path_value(param.arg, value)
                )
            case ParamLocation.QUERY:
                query.extend(_query_pairs(param.wire_name, value, param.explode))
            case ParamLocation.HEADER:
                headers[param.wire_name] = _check_header_value(param.wire_name, str(value))

    body: bytes | None = None
    if binding.body is not None:
        if binding.body.mode is BodyMode.SINGLE_ARG or binding.body.schema.get("type") != "object":
            if "body" not in arguments:
                raise RequestBuildError(f"missing required argument {'body'!r}")
            body = json.dumps(arguments["body"]).encode()
        else:
            declared = set(binding.body.schema.get("properties", {}))
            body_fields = {k: v for k, v in arguments.items() if k in declared}
            for name in binding.body.schema.get("required", []):
                if name not in arguments or name not in body_fields:
                    raise RequestBuildError(f"missing required argument {name!r}")
            body = json.dumps(body_fields).encode()
        if body is not None:
            headers["Content-Type"] = binding.body.content_type

    url = _join(base_url, path)
    if query:
        url = f"{url}?{urlencode(query)}"

    # Attached last so a model-supplied header can never displace the real credential.
    if credential is not None:
        headers[credential.header] = credential.value

    return PreparedRequest(method=binding.method, url=url, headers=headers, body=body)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    text: str
    truncated: bool
    original_bytes: int


class HttpTransport:
    def __init__(
        self,
        client: httpx.AsyncClient,
        upstream: UpstreamConfig,
        credential_source: CredentialSource,
    ) -> None:
        self._client = client
        self._upstream = upstream
        self._credential_source = credential_source

    def _retryable(self, operation: Operation) -> bool:
        # Retrying a POST after a timeout is how a customer gets charged twice.
        return operation.effect in (Effect.READ_ONLY, Effect.IDEMPOTENT_WRITE)

    async def _wait_for_retry(
        self, started: float, attempt: int, response: httpx.Response | None
    ) -> bool:
        return await wait_for_retry(
            started=started,
            attempt=attempt,
            response=response,
            max_total_ms=self._upstream.max_total_ms,
            per_attempt_timeout_s=self._upstream.timeout_ms / 1000,
        )

    def _map(self, response: httpx.Response) -> HttpResponse:
        raw = response.content
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        text, truncated = map_body(
            raw, content_type, self._upstream.max_response_bytes, decoded_text=response.text
        )
        return HttpResponse(
            status=response.status_code, text=text, truncated=truncated, original_bytes=len(raw)
        )

    async def execute(
        self,
        operation: Operation,
        arguments: dict[str, Any],
        carry: tuple[AuthorizationDetail, ...] = (),
        subject_token: str | None = None,
        subject_token_expires_at: int | None = None,
    ) -> HttpResponse:
        binding = operation.binding
        assert isinstance(binding, HttpBinding)
        base_url = self._upstream.base_url
        # Callers must resolve base_url before constructing HttpTransport.
        assert base_url is not None
        credential = await self._credential_source.get(
            carry, subject_token, subject_token_expires_at
        )
        request = build_request(binding, base_url, arguments, credential)

        attempts = MAX_ATTEMPTS if self._retryable(operation) else 1
        last: httpx.Response | None = None
        started = time.monotonic()

        for attempt in range(attempts):
            try:
                last = await self._client.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    content=request.body,
                    timeout=self._upstream.timeout_ms / 1000,
                )
            except httpx.TimeoutException:
                if attempt == attempts - 1 or not await self._wait_for_retry(
                    started, attempt, None
                ):
                    raise
                continue

            retryable = is_retryable_status(last.status_code)
            if not retryable or attempt == attempts - 1:
                return self._map(last)
            if not await self._wait_for_retry(started, attempt, last):
                return self._map(last)

        assert last is not None
        return self._map(last)
