"""Build and execute HTTP requests for an HttpBinding.

`build_request` is pure and is the highest-risk code in the system: it is the
boundary where model-supplied values become a real request.
"""

import asyncio
import json
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from mcp_portal.config.models import UpstreamConfig
from mcp_portal.operations import BodyMode, Effect, HttpBinding, Operation, ParamLocation

_CRLF = ("\r", "\n")

RETRYABLE_STATUS = frozenset({502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 0.1
_RETRY_AFTER_CAP_S = 10.0

_TEXTUAL_SUFFIXES = ("+json", "+xml")
_TEXTUAL_TYPES = frozenset({"application/json", "application/xml", "application/yaml"})


class RequestBuildError(Exception):
    """Raised when validated arguments still cannot form a safe request."""


@dataclass(frozen=True, slots=True)
class Credential:
    header: str
    value: str


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


def _is_textual(content_type: str) -> bool:
    """Whether a response body is safe to inline as text.

    An empty content type is treated as textual: httpx.MockTransport and many real
    APIs omit it on small JSON bodies, and inlining a short unknown body is less
    harmful than hiding a real one.
    """
    if not content_type:
        return True
    if content_type.startswith("text/"):
        return True
    return content_type in _TEXTUAL_TYPES or content_type.endswith(_TEXTUAL_SUFFIXES)


def _retry_delay_s(attempt: int, response: httpx.Response | None) -> float:
    """How long to wait before the attempt after this one."""
    if response is not None and response.status_code == 429:
        header = response.headers.get("retry-after")
        if header and header.isdigit():
            return min(float(header), _RETRY_AFTER_CAP_S)
    backoff = _BACKOFF_BASE_S * (2**attempt)
    return random.uniform(0, backoff)  # noqa: S311 - jitter, not crypto


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
        credential: Credential | None,
    ) -> None:
        self._client = client
        self._upstream = upstream
        self._credential = credential

    def _retryable(self, operation: Operation) -> bool:
        # Retrying a POST after a timeout is how a customer gets charged twice.
        return operation.effect in (Effect.READ_ONLY, Effect.IDEMPOTENT_WRITE)

    def _remaining_s(self, started: float) -> float | None:
        """Seconds left in the total wall-clock budget, or None when it is unbounded."""
        budget_ms = self._upstream.max_total_ms
        if budget_ms is None:
            return None
        return budget_ms / 1000 - (time.monotonic() - started)

    async def _wait_for_retry(
        self, started: float, attempt: int, response: httpx.Response | None
    ) -> bool:
        """Wait before the next attempt; False when the budget cannot fund one.

        `timeout_ms` bounds one attempt; `max_total_ms` bounds the retry loop as a
        whole, so the wait and the attempt it buys have to fit in what is left of
        the budget together. Asking only whether the budget was already spent let
        a `Retry-After` of several seconds be slept off against a budget of a few
        hundred milliseconds, and let three attempts plus backoff hold a caller
        well past the total the operator configured.
        """
        delay = _retry_delay_s(attempt, response)
        remaining = self._remaining_s(started)
        # Below the gate `delay` is already strictly under `remaining`, so the
        # sleep needs no separate clamp: the budget bounds it by construction.
        if remaining is not None and delay + self._upstream.timeout_ms / 1000 > remaining:
            return False
        await asyncio.sleep(delay)
        return True

    def _map(self, response: httpx.Response) -> HttpResponse:
        raw = response.content
        content_type = response.headers.get("content-type", "").split(";")[0].strip()

        # Binary is described, never inlined: an error path or a stray image
        # endpoint must not be able to dump base64 into a context window.
        if not _is_textual(content_type):
            return HttpResponse(
                status=response.status_code,
                text=f"[{len(raw)} bytes of {content_type or 'unknown content type'}, not inlined]",
                truncated=False,
                original_bytes=len(raw),
            )

        cap = self._upstream.max_response_bytes
        if len(raw) <= cap:
            return HttpResponse(response.status_code, response.text, False, len(raw))
        body = raw[:cap].decode(errors="replace")
        marker = f"\n\n[truncated: {len(raw)} bytes total, {cap} shown]"
        return HttpResponse(response.status_code, body + marker, True, len(raw))

    async def execute(self, operation: Operation, arguments: dict[str, Any]) -> HttpResponse:
        binding = operation.binding
        assert isinstance(binding, HttpBinding)
        base_url = self._upstream.base_url
        # Callers must resolve base_url before constructing HttpTransport.
        assert base_url is not None
        request = build_request(binding, base_url, arguments, self._credential)

        attempts = _MAX_ATTEMPTS if self._retryable(operation) else 1
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

            retryable = last.status_code in RETRYABLE_STATUS or last.status_code == 429
            if not retryable or attempt == attempts - 1:
                return self._map(last)
            if not await self._wait_for_retry(started, attempt, last):
                return self._map(last)

        assert last is not None
        return self._map(last)
