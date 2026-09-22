"""A single-flighted, TTL-based cache for tokens acquired from an IdP.

Shared by every dynamic outbound mode (`client_credentials` now,
`token_exchange` later): both mint a token good for a window, keyed by
whatever the caller derives from upstream + scopes + carried details (and,
for exchange, the subject token). A burst of concurrent tool calls for the
same key must trigger exactly one IdP request, not one per caller — hence
the per-key lock rather than a bare dict.
"""

import time
from asyncio import Lock
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _CachedToken:
    value: str
    expires_at: float  # a time.monotonic() timestamp


class TokenCache:
    def __init__(self) -> None:
        self._entries: dict[str, _CachedToken] = {}
        self._locks: dict[str, Lock] = {}

    def _lock_for(self, key: str) -> Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = Lock()
            self._locks[key] = lock
        return lock

    async def get_or_fetch(
        self, key: str, fetch: Callable[[], Awaitable[tuple[str, float]]]
    ) -> str:
        """Return the cached token for `key`, fetching it if absent or expired.

        `fetch` returns `(token, ttl_seconds)`. Only the first caller to reach
        an absent-or-expired entry ever awaits `fetch`; every concurrent
        caller for the same key blocks on the same lock and then reads the
        entry that call just populated.
        """
        cached = self._entries.get(key)
        if cached is not None and cached.expires_at > time.monotonic():
            return cached.value

        async with self._lock_for(key):
            cached = self._entries.get(key)
            if cached is not None and cached.expires_at > time.monotonic():
                return cached.value
            token, ttl_seconds = await fetch()
            self._entries[key] = _CachedToken(
                value=token, expires_at=time.monotonic() + ttl_seconds
            )
            self._purge_expired(exclude=key)
            return token

    def _purge_expired(self, exclude: str) -> None:
        """Drop every entry (and its lock) other than `exclude` whose TTL has
        passed. Piggybacks on the already-locked `get_or_fetch` path rather
        than running on a timer, so `client_credentials`'s small, bounded key
        space costs nothing extra and `token_exchange`'s per-caller keys
        don't accumulate forever on a long-running server.
        """
        now = time.monotonic()
        expired = [
            k for k, entry in self._entries.items() if k != exclude and entry.expires_at <= now
        ]
        for k in expired:
            del self._entries[k]
            lock = self._locks.get(k)
            # A held lock means another caller is mid-`fetch()` for this key;
            # dropping it here would let a still-later caller create a fresh
            # Lock and race that in-flight fetch instead of waiting on it.
            if lock is not None and not lock.locked():
                del self._locks[k]
