"""Process-local conversation metadata and API execution coordination."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


DEFAULT_LOCAL_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    """UI-independent conversation metadata."""

    id: uuid.UUID
    user_id: uuid.UUID
    thread_id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class NoteRecord:
    """A note owned by one user and conversation."""

    id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    title: str
    content: str
    created_at: datetime
    updated_at: datetime


class InMemoryConversationStore:
    """Store conversation metadata for the lifetime of one API process."""

    def __init__(self) -> None:
        self._records: dict[uuid.UUID, ConversationRecord] = {}
        self._users: dict[str, uuid.UUID] = {}
        self._lock = asyncio.Lock()

    async def ensure_user(
        self,
        user_id: uuid.UUID,
        external_subject: str,
    ) -> None:
        """Accept the compatibility user without external persistence."""
        async with self._lock:
            self._users.setdefault(external_subject, user_id)

    async def get_or_create_user(
        self,
        external_subject: str,
        *,
        display_name: str | None = None,
    ) -> uuid.UUID:
        """Resolve a stable internal user ID for an external OIDC subject."""
        del display_name
        async with self._lock:
            return self._users.setdefault(external_subject, uuid.uuid4())

    async def create(
        self,
        *,
        user_id: uuid.UUID = DEFAULT_LOCAL_USER_ID,
        title: str = "新しい会話",
        thread_id: str | None = None,
        retention_days: int = 90,
    ) -> ConversationRecord:
        """Create and retain a new conversation record."""
        conversation_id = uuid.uuid4()
        now = datetime.now(UTC)
        record = ConversationRecord(
            id=conversation_id,
            user_id=user_id,
            thread_id=thread_id or str(conversation_id),
            title=title,
            status="active",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(days=retention_days),
        )
        async with self._lock:
            self._records[record.id] = record
        return record

    async def get(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID = DEFAULT_LOCAL_USER_ID,
    ) -> ConversationRecord | None:
        """Return a conversation record when it exists in this process."""
        async with self._lock:
            record = self._records.get(conversation_id)
            return record if record and record.user_id == user_id else None

    async def get_by_thread_id(
        self,
        thread_id: str,
        *,
        user_id: uuid.UUID = DEFAULT_LOCAL_USER_ID,
    ) -> ConversationRecord | None:
        """Return metadata for a persisted LangGraph thread ID."""
        async with self._lock:
            return next(
                (
                    record
                    for record in self._records.values()
                    if record.thread_id == thread_id and record.user_id == user_id
                ),
                None,
            )

    async def list(
        self,
        *,
        user_id: uuid.UUID = DEFAULT_LOCAL_USER_ID,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ConversationRecord], int]:
        """List a user's conversations, newest first."""
        async with self._lock:
            records = sorted(
                (
                    record
                    for record in self._records.values()
                    if record.user_id == user_id
                ),
                key=lambda record: record.updated_at,
                reverse=True,
            )
            return records[offset : offset + limit], len(records)

    async def update(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID = DEFAULT_LOCAL_USER_ID,
        title: str | None = None,
        status: str | None = None,
    ) -> ConversationRecord | None:
        """Update mutable conversation metadata."""
        async with self._lock:
            current = self._records.get(conversation_id)
            if current is None or current.user_id != user_id:
                return None
            record = ConversationRecord(
                id=current.id,
                user_id=current.user_id,
                thread_id=current.thread_id,
                title=title if title is not None else current.title,
                status=status if status is not None else current.status,
                created_at=current.created_at,
                updated_at=datetime.now(UTC),
                expires_at=current.expires_at,
            )
            self._records[conversation_id] = record
            return record

    async def touch(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID = DEFAULT_LOCAL_USER_ID,
    ) -> None:
        """Update the last activity timestamp."""
        await self.update(conversation_id, user_id=user_id)

    async def delete(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID = DEFAULT_LOCAL_USER_ID,
    ) -> bool:
        """Delete a conversation owned by the user."""
        async with self._lock:
            record = self._records.get(conversation_id)
            if record is None or record.user_id != user_id:
                return False
            del self._records[conversation_id]
            return True

    async def list_expired(self, *, limit: int = 100) -> list[ConversationRecord]:
        """List expired conversations for retention cleanup."""
        now = datetime.now(UTC)
        async with self._lock:
            return [
                record
                for record in self._records.values()
                if record.expires_at <= now
            ][:limit]


class InMemoryNoteStore:
    """Process-local note storage used when PostgreSQL is not configured."""

    def __init__(self) -> None:
        self._records: dict[uuid.UUID, NoteRecord] = {}
        self._lock = asyncio.Lock()

    async def list(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID = DEFAULT_LOCAL_USER_ID,
    ) -> list[NoteRecord]:
        async with self._lock:
            return sorted(
                (
                    note
                    for note in self._records.values()
                    if note.conversation_id == conversation_id
                    and note.user_id == user_id
                ),
                key=lambda note: note.created_at,
            )

    async def create(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID = DEFAULT_LOCAL_USER_ID,
        title: str,
        content: str,
    ) -> NoteRecord:
        now = datetime.now(UTC)
        note = NoteRecord(
            id=uuid.uuid4(),
            user_id=user_id,
            conversation_id=conversation_id,
            title=title,
            content=content,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._records[note.id] = note
        return note

    async def update(
        self,
        conversation_id: uuid.UUID,
        note_id: uuid.UUID,
        *,
        user_id: uuid.UUID = DEFAULT_LOCAL_USER_ID,
        title: str | None = None,
        content: str | None = None,
    ) -> NoteRecord | None:
        async with self._lock:
            current = self._records.get(note_id)
            if (
                current is None
                or current.user_id != user_id
                or current.conversation_id != conversation_id
            ):
                return None
            note = NoteRecord(
                id=current.id,
                user_id=current.user_id,
                conversation_id=current.conversation_id,
                title=title if title is not None else current.title,
                content=content if content is not None else current.content,
                created_at=current.created_at,
                updated_at=datetime.now(UTC),
            )
            self._records[note_id] = note
            return note

    async def delete(
        self,
        conversation_id: uuid.UUID,
        note_id: uuid.UUID,
        *,
        user_id: uuid.UUID = DEFAULT_LOCAL_USER_ID,
    ) -> bool:
        async with self._lock:
            note = self._records.get(note_id)
            if (
                note is None
                or note.user_id != user_id
                or note.conversation_id != conversation_id
            ):
                return False
            del self._records[note_id]
            return True

    async def delete_for_conversation(self, conversation_id: uuid.UUID) -> None:
        async with self._lock:
            note_ids = [
                note_id
                for note_id, note in self._records.items()
                if note.conversation_id == conversation_id
            ]
            for note_id in note_ids:
                del self._records[note_id]


class ConversationExecutionRegistry:
    """Prevent overlapping agent runs for the same conversation."""

    def __init__(self) -> None:
        self._active: set[uuid.UUID] = set()
        self._cancel_requested: set[uuid.UUID] = set()
        self._lock = asyncio.Lock()

    async def reserve(self, conversation_id: uuid.UUID) -> bool:
        """Atomically reserve a conversation, returning false when it is busy."""
        async with self._lock:
            if conversation_id in self._active:
                return False
            self._active.add(conversation_id)
            self._cancel_requested.discard(conversation_id)
            return True

    async def release(self, conversation_id: uuid.UUID) -> None:
        """Release a previously reserved conversation."""
        async with self._lock:
            self._active.discard(conversation_id)
            self._cancel_requested.discard(conversation_id)

    async def request_cancel(self, conversation_id: uuid.UUID) -> bool:
        """Request cancellation when a conversation is currently running."""
        async with self._lock:
            if conversation_id not in self._active:
                return False
            self._cancel_requested.add(conversation_id)
            return True

    async def is_cancel_requested(self, conversation_id: uuid.UUID) -> bool:
        """Return whether cancellation was requested for the active run."""
        async with self._lock:
            return conversation_id in self._cancel_requested


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

    async def delete_resource(self, resource_id: str) -> None:
        """Delete cached responses owned by a removed resource."""
        async with self._lock:
            keys = [
                key for key in self._entries if key[1] == resource_id
            ]
            for key in keys:
                self._entries.pop(key, None)
