"""Shared fixed-window rate-limit primitives."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of one rate-limit counter increment."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    window_seconds: int


class InMemoryRateLimiter:
    """Process-local fixed-window limiter used without PostgreSQL."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str, int], int] = {}
        self._lock = asyncio.Lock()

    async def hit(
        self,
        scope: str,
        subject: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> RateLimitResult:
        now = int(time.time())
        bucket = now - (now % window_seconds)
        key = (scope, hash_identifier(subject), bucket)
        async with self._lock:
            self._buckets[key] = self._buckets.get(key, 0) + 1
            count = self._buckets[key]
            if len(self._buckets) > 10_000:
                oldest = bucket - window_seconds
                self._buckets = {
                    item: value
                    for item, value in self._buckets.items()
                    if item[2] >= oldest
                }
        return build_rate_limit_result(
            count,
            limit,
            window_seconds - (now - bucket),
            window_seconds,
        )


def build_rate_limit_result(
    count: int,
    limit: int,
    retry_after: int,
    window_seconds: int,
) -> RateLimitResult:
    return RateLimitResult(
        allowed=count <= limit,
        limit=limit,
        remaining=max(0, limit - count),
        retry_after=max(1, retry_after),
        window_seconds=window_seconds,
    )


def hash_identifier(value: str) -> str:
    """Pseudonymize an address or user identifier before persistence/logging."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
