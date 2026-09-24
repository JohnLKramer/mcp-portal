"""Build and execute a GraphQL request for a GraphQlBinding.

GraphQL-over-HTTP needs no path templating: one POST, a JSON body of
`{"query": ..., "variables": ...}`. Retry/backoff and the response-size cap
are the same rules `HttpTransport` uses, imported from `transports/common.py`
rather than re-derived.
"""

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from mcp_portal.auth.rar import AuthorizationDetail
from mcp_portal.config.models import UpstreamConfig
from mcp_portal.operations import Effect, GraphQlBinding, Operation
from mcp_portal.transports.common import MAX_ATTEMPTS, is_retryable_status, map_body, wait_for_retry
from mcp_portal.transports.http import Credential, CredentialSource, PreparedRequest


def build_graphql_request(
    binding: GraphQlBinding, endpoint: str, arguments: dict[str, Any], credential: Credential | None
) -> PreparedRequest:
    variables = {v.name: arguments[v.name] for v in binding.variables if v.name in arguments}
    headers = {"Content-Type": "application/json"}
    if credential is not None:
        headers[credential.header] = credential.value
    body = json.dumps({"query": binding.document, "variables": variables}).encode()
    return PreparedRequest(method="POST", url=endpoint, headers=headers, body=body)


@dataclass(frozen=True, slots=True)
class GraphQlResponse:
    status: int
    text: str
    truncated: bool
    original_bytes: int


class GraphQlTransport:
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
        return operation.effect in (Effect.READ_ONLY, Effect.IDEMPOTENT_WRITE)

    def _map(self, response: httpx.Response) -> GraphQlResponse:
        raw = response.content
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        text, truncated = map_body(
            raw, content_type, self._upstream.max_response_bytes, decoded_text=response.text
        )

        # GraphQL responds 200 even on failure, with `errors` alongside or
        # instead of `data`. Remapped to a synthetic non-2xx status so
        # server/mcp.py's existing `is_error = not (2xx)` check classifies it
        # correctly without any GraphQL-specific branching there.
        #
        # This check is against the raw body, not `text`/`truncated`: a large
        # or non-textual error response must still fail the call, even though
        # its *displayed* text ends up truncated or replaced with a
        # placeholder. "GraphQL errors always mean failure" cannot depend on
        # how much of the body happened to fit under the size cap.
        status = response.status_code
        if status == 200:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("errors"):
                status = 502
        return GraphQlResponse(
            status=status, text=text, truncated=truncated, original_bytes=len(raw)
        )

    async def execute(
        self,
        operation: Operation,
        arguments: dict[str, Any],
        carry: tuple[AuthorizationDetail, ...] = (),
        subject_token: str | None = None,
        subject_token_expires_at: int | None = None,
    ) -> GraphQlResponse:
        binding = operation.binding
        assert isinstance(binding, GraphQlBinding)
        base_url = self._upstream.base_url
        assert base_url is not None
        credential = await self._credential_source.get(
            carry, subject_token, subject_token_expires_at
        )
        request = build_graphql_request(binding, base_url, arguments, credential)

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

            if not is_retryable_status(last.status_code) or attempt == attempts - 1:
                return self._map(last)
            if not await self._wait_for_retry(started, attempt, last):
                return self._map(last)

        assert last is not None
        return self._map(last)

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
