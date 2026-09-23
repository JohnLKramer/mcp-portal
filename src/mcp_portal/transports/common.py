"""Retry timing and response-mapping helpers shared by every transport.

Pulled out of `transports/http.py` when `transports/graphql.py` needed the
identical effect-gated retry and response-size-cap behavior — duplicating
~40 lines of retry/backoff math per protocol was the alternative, and this
is the one place that logic should exist.
"""

import random

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


def retry_delay_s(attempt: int, retry_after_header: str | None) -> float:
    if retry_after_header and retry_after_header.isdigit():
        return min(float(retry_after_header), RETRY_AFTER_CAP_S)
    backoff = _BACKOFF_BASE_S * (2**attempt)
    return random.uniform(0, backoff)  # noqa: S311 - jitter, not crypto


def map_body(raw: bytes, content_type: str, cap: int) -> tuple[str, bool]:
    """Return `(text, truncated)` for a response body, honoring the same
    textual/binary and size-cap rules for every transport."""
    if not is_textual(content_type):
        return f"[{len(raw)} bytes of {content_type or 'unknown content type'}, not inlined]", False
    if len(raw) <= cap:
        return raw.decode(errors="replace"), False
    body = raw[:cap].decode(errors="replace")
    return body + f"\n\n[truncated: {len(raw)} bytes total, {cap} shown]", True
