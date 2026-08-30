"""Process-local conversation metadata and API execution coordination."""

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    """Minimal conversation metadata used before the PostgreSQL phase."""

    id: uuid.UUID
    created_at: datetime


class InMemoryConversationStore:
    """Store conversation IDs for the lifetime of one API process."""

    def __init__(self) -> None:
        self._records: dict[uuid.UUID, ConversationRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self) -> ConversationRecord:
        """Create and retain a new conversation record."""
        record = ConversationRecord(id=uuid.uuid4(), created_at=datetime.now(UTC))
        async with self._lock:
            self._records[record.id] = record
        return record

    async def get(self, conversation_id: uuid.UUID) -> ConversationRecord | None:
        """Return a conversation record when it exists in this process."""
        async with self._lock:
            return self._records.get(conversation_id)


class ConversationExecutionRegistry:
    """Prevent overlapping agent runs for the same conversation."""

    def __init__(self) -> None:
        self._active: set[uuid.UUID] = set()
        self._lock = asyncio.Lock()

    async def reserve(self, conversation_id: uuid.UUID) -> bool:
        """Atomically reserve a conversation, returning false when it is busy."""
        async with self._lock:
            if conversation_id in self._active:
                return False
            self._active.add(conversation_id)
            return True

    async def release(self, conversation_id: uuid.UUID) -> None:
        """Release a previously reserved conversation."""
        async with self._lock:
            self._active.discard(conversation_id)


@dataclass(slots=True)
class _IdempotencyEntry:
    fingerprint: str
    value: Any
    expires_at: float


class IdempotencyStore:
    """Bounded process-local cache for completed idempotent requests."""

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str, str], _IdempotencyEntry] = (
            OrderedDict()
        )
        self._lock = asyncio.Lock()

    def _remove_expired(self, now: float) -> None:
        expired = [
            key for key, entry in self._entries.items() if entry.expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)

    async def get(
        self,
        scope: str,
        resource_id: str,
        key: str,
    ) -> tuple[str, Any] | None:
        """Return a cached value when it has not expired."""
        cache_key = (scope, resource_id, key)
        async with self._lock:
            self._remove_expired(time.monotonic())
            entry = self._entries.get(cache_key)
            if entry is None:
                return None
            self._entries.move_to_end(cache_key)
            return entry.fingerprint, entry.value

    async def put(
        self,
        scope: str,
        resource_id: str,
        key: str,
        fingerprint: str,
        value: Any,
    ) -> None:
        """Store a completed value and evict the oldest entries when bounded."""
        cache_key = (scope, resource_id, key)
        async with self._lock:
            now = time.monotonic()
            self._remove_expired(now)
            self._entries[cache_key] = _IdempotencyEntry(
                fingerprint=fingerprint,
                value=value,
                expires_at=now + self.ttl_seconds,
            )
            self._entries.move_to_end(cache_key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
