"""Retry timing and response-mapping helpers shared by every transport.

Pulled out of `transports/http.py` when `transports/graphql.py` needed the
identical effect-gated retry and response-size-cap behavior — duplicating
~40 lines of retry/backoff math per protocol was the alternative, and this
is the one place that logic should exist.
"""

import asyncio
import random
import time

import httpx

RETRYABLE_STATUS = frozenset({502, 503, 504})
MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 0.1
RETRY_AFTER_CAP_S = 10.0

_TEXTUAL_SUFFIXES = ("+json", "+xml")
_TEXTUAL_TYPES = frozenset({"application/json", "application/xml", "application/yaml"})


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS or status == 429


def is_textual(content_type: str) -> bool:
    if not content_type:
        return True
    if content_type.startswith("text/"):
        return True
    return content_type in _TEXTUAL_TYPES or content_type.endswith(_TEXTUAL_SUFFIXES)


def retry_delay_s(attempt: int, status_code: int | None, retry_after_header: str | None) -> float:
    """Retry-After is only trustworthy on a 429: a rate limiter is telling the
    caller precisely when it will accept traffic again. On a 502/503/504 the
    header (when even present) reflects an intermediary's guess, not a
    guarantee, so those statuses always fall back to jittered backoff."""
    if status_code == 429 and retry_after_header and retry_after_header.isdigit():
        return min(float(retry_after_header), RETRY_AFTER_CAP_S)
    backoff = _BACKOFF_BASE_S * (2**attempt)
    return random.uniform(0, backoff)  # noqa: S311 - jitter, not crypto


def map_body(
    raw: bytes, content_type: str, cap: int, decoded_text: str | None = None
) -> tuple[str, bool]:
    """Return `(text, truncated)` for a response body, honoring the same
    textual/binary and size-cap rules for every transport.

    `decoded_text` is an optional charset-aware decode of the full body (e.g.
    httpx's `response.text`, which honors a `charset=` on the Content-Type
    header or httpx's own encoding detection). It is used verbatim when the
    body fits under `cap`. The truncated path still decodes a raw byte slice
    with `errors="replace"`, since a pre-decoded string can't be re-sliced by
    byte offset without risking a split multi-byte character.
    """
    if not is_textual(content_type):
        return f"[{len(raw)} bytes of {content_type or 'unknown content type'}, not inlined]", False
    if len(raw) <= cap:
        return (decoded_text if decoded_text is not None else raw.decode(errors="replace")), False
    body = raw[:cap].decode(errors="replace")
    return body + f"\n\n[truncated: {len(raw)} bytes total, {cap} shown]", True


def remaining_budget_s(started: float, budget_ms: int | None) -> float | None:
    """Seconds left in a `max_total_ms` wall-clock budget, or None when
    unbounded. Shared so `max_total_ms` means the same thing for every
    transport's retry loop, not just the one it was first written for."""
    if budget_ms is None:
        return None
    return budget_ms / 1000 - (time.monotonic() - started)


async def wait_for_retry(
    *,
    started: float,
    attempt: int,
    response: httpx.Response | None,
    max_total_ms: int | None,
    per_attempt_timeout_s: float,
) -> bool:
    """Wait before the next retry attempt; False when the budget cannot fund one.

    `per_attempt_timeout_s` bounds one attempt; `max_total_ms` bounds the
    retry loop as a whole, so the wait and the attempt it buys have to fit in
    what is left of the budget together. Asking only whether the budget was
    already spent would let a `Retry-After` of several seconds be slept off
    against a budget of a few hundred milliseconds, and let several attempts
    plus backoff hold a caller well past the total the operator configured.
    """
    header = response.headers.get("retry-after") if response is not None else None
    status_code = response.status_code if response is not None else None
    delay = retry_delay_s(attempt, status_code, header)
    remaining = remaining_budget_s(started, max_total_ms)
    # Below the gate `delay` is already strictly under `remaining`, so the
    # sleep needs no separate clamp: the budget bounds it by construction.
    if remaining is not None and delay + per_attempt_timeout_s > remaining:
        return False
    await asyncio.sleep(delay)
    return True
