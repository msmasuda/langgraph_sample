"""PostgreSQL-backed repositories for conversations and API coordination."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models import (
    Conversation,
    ConversationExecution,
    IdempotencyRecord,
    Note,
    RateLimitBucket,
    ToolExecution,
    UsageRecord,
    User,
)
from src.services.conversation_service import ConversationRecord, NoteRecord
from src.services.rate_limit import (
    RateLimitResult,
    build_rate_limit_result,
    hash_identifier,
)


def _conversation_record(row: Conversation) -> ConversationRecord:
    return ConversationRecord(
        id=row.id,
        user_id=row.user_id,
        thread_id=row.thread_id,
        title=row.title,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
    )


def _note_record(row: Note) -> NoteRecord:
    return NoteRecord(
        id=row.id,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        title=row.title,
        content=row.content,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class DatabaseRateLimiter:
    """Share fixed-window counters across all PostgreSQL API processes."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self._last_cleanup = 0.0
        self._cleanup_lock = asyncio.Lock()

    async def hit(
        self,
        scope: str,
        subject: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> RateLimitResult:
        now = datetime.now(UTC)
        epoch = int(now.timestamp())
        bucket_epoch = epoch - (epoch % window_seconds)
        bucket_start = datetime.fromtimestamp(bucket_epoch, UTC)
        expires_at = bucket_start + timedelta(seconds=window_seconds * 2)
        statement = (
            postgres_insert(RateLimitBucket)
            .values(
                scope=scope,
                subject_hash=hash_identifier(subject),
                window_started_at=bucket_start,
                request_count=1,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                index_elements=[
                    RateLimitBucket.scope,
                    RateLimitBucket.subject_hash,
                    RateLimitBucket.window_started_at,
                ],
                set_={
                    "request_count": RateLimitBucket.request_count + 1,
                    "expires_at": expires_at,
                },
            )
            .returning(RateLimitBucket.request_count)
        )
        async with self.sessions() as session:
            count = await session.scalar(statement)
            await session.commit()
        await self._cleanup_if_due(now, window_seconds)
        return build_rate_limit_result(
            int(count or 1),
            limit,
            window_seconds - (epoch - bucket_epoch),
            window_seconds,
        )

    async def _cleanup_if_due(self, now: datetime, window_seconds: int) -> None:
        monotonic_now = asyncio.get_running_loop().time()
        if monotonic_now - self._last_cleanup < window_seconds:
            return
        async with self._cleanup_lock:
            if monotonic_now - self._last_cleanup < window_seconds:
                return
            async with self.sessions() as session:
                await session.execute(
                    delete(RateLimitBucket).where(RateLimitBucket.expires_at < now)
                )
                await session.commit()
            self._last_cleanup = monotonic_now


class DatabaseConversationStore:
    """Persist user-owned conversation metadata in PostgreSQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def ensure_user(self, user_id: uuid.UUID, external_subject: str) -> None:
        """Create the compatibility user once before authentication is added."""
        now = datetime.now(UTC)
        async with self.sessions() as session:
            if await session.get(User, user_id) is not None:
                return
            session.add(
                User(
                    id=user_id,
                    external_subject=external_subject,
                    display_name="ローカルユーザー",
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()

    async def get_or_create_user(
        self,
        external_subject: str,
        *,
        display_name: str | None = None,
    ) -> uuid.UUID:
        """Resolve an OIDC subject to one stable internal user ID."""
        now = datetime.now(UTC)
        candidate_id = uuid.uuid4()
        statement = (
            postgres_insert(User)
            .values(
                id=candidate_id,
                external_subject=external_subject,
                display_name=display_name,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[User.external_subject])
        )
        async with self.sessions() as session:
            await session.execute(statement)
            await session.commit()
            user_id = await session.scalar(
                select(User.id).where(User.external_subject == external_subject)
            )
        if user_id is None:
            raise RuntimeError("OIDCユーザーを作成できませんでした")
        return user_id

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        title: str = "新しい会話",
        thread_id: str | None = None,
        retention_days: int = 90,
    ) -> ConversationRecord:
        conversation_id = uuid.uuid4()
        now = datetime.now(UTC)
        row = Conversation(
            id=conversation_id,
            user_id=user_id,
            thread_id=thread_id or str(conversation_id),
            title=title,
            status="active",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(days=retention_days),
        )
        async with self.sessions() as session:
            session.add(row)
            await session.commit()
        return _conversation_record(row)

    async def get(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
    ) -> ConversationRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                )
            )
        return _conversation_record(row) if row else None

    async def get_by_thread_id(
        self,
        thread_id: str,
        *,
        user_id: uuid.UUID,
    ) -> ConversationRecord | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(Conversation).where(
                    Conversation.thread_id == thread_id,
                    Conversation.user_id == user_id,
                )
            )
        return _conversation_record(row) if row else None

    async def list(
        self,
        *,
        user_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ConversationRecord], int]:
        async with self.sessions() as session:
            total = await session.scalar(
                select(func.count()).select_from(Conversation).where(
                    Conversation.user_id == user_id
                )
            )
            rows = (
                await session.scalars(
                    select(Conversation)
                    .where(Conversation.user_id == user_id)
                    .order_by(Conversation.updated_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return [_conversation_record(row) for row in rows], int(total or 0)

    async def update(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        title: str | None = None,
        status: str | None = None,
    ) -> ConversationRecord | None:
        values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
        if title is not None:
            values["title"] = title
        if status is not None:
            values["status"] = status
        async with self.sessions() as session:
            row = await session.scalar(
                update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                )
                .values(**values)
                .returning(Conversation)
            )
            await session.commit()
        return _conversation_record(row) if row else None

    async def touch(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
    ) -> None:
        await self.update(conversation_id, user_id=user_id)

    async def delete(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
    ) -> bool:
        async with self.sessions() as session:
            result = await session.execute(
                delete(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                )
            )
            await session.commit()
        return bool(result.rowcount)

    async def list_expired(self, *, limit: int = 100) -> list[ConversationRecord]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(Conversation)
                    .where(Conversation.expires_at <= datetime.now(UTC))
                    .order_by(Conversation.expires_at)
                    .limit(limit)
                )
            ).all()
        return [_conversation_record(row) for row in rows]


class DatabaseNoteStore:
    """Persist conversation notes in PostgreSQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def list(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
    ) -> list[NoteRecord]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(Note)
                    .where(
                        Note.conversation_id == conversation_id,
                        Note.user_id == user_id,
                    )
                    .order_by(Note.created_at)
                )
            ).all()
        return [_note_record(row) for row in rows]

    async def create(
        self,
        conversation_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        title: str,
        content: str,
    ) -> NoteRecord:
        now = datetime.now(UTC)
        row = Note(
            id=uuid.uuid4(),
            user_id=user_id,
            conversation_id=conversation_id,
            title=title,
            content=content,
            created_at=now,
            updated_at=now,
        )
        async with self.sessions() as session:
            session.add(row)
            await session.commit()
        return _note_record(row)

    async def update(
        self,
        conversation_id: uuid.UUID,
        note_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        title: str | None = None,
        content: str | None = None,
    ) -> NoteRecord | None:
        values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
        if title is not None:
            values["title"] = title
        if content is not None:
            values["content"] = content
        async with self.sessions() as session:
            row = await session.scalar(
                update(Note)
                .where(
                    Note.id == note_id,
                    Note.conversation_id == conversation_id,
                    Note.user_id == user_id,
                )
                .values(**values)
                .returning(Note)
            )
            await session.commit()
        return _note_record(row) if row else None

    async def delete(
        self,
        conversation_id: uuid.UUID,
        note_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
    ) -> bool:
        async with self.sessions() as session:
            result = await session.execute(
                delete(Note).where(
                    Note.id == note_id,
                    Note.conversation_id == conversation_id,
                    Note.user_id == user_id,
                )
            )
            await session.commit()
        return bool(result.rowcount)

    async def delete_for_conversation(self, conversation_id: uuid.UUID) -> None:
        async with self.sessions() as session:
            await session.execute(
                delete(Note).where(Note.conversation_id == conversation_id)
            )
            await session.commit()


class PostgresConversationExecutionRegistry:
    """Coordinate one active run per conversation across API processes."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        lease_seconds: int,
    ) -> None:
        self.sessions = sessions
        self.lease_seconds = lease_seconds
        self._owners: dict[uuid.UUID, uuid.UUID] = {}
        self._owners_lock = asyncio.Lock()

    async def reserve(self, conversation_id: uuid.UUID) -> bool:
        now = datetime.now(UTC)
        owner_token = uuid.uuid4()
        statement = (
            postgres_insert(ConversationExecution)
            .values(
                conversation_id=conversation_id,
                owner_token=owner_token,
                cancel_requested=False,
                acquired_at=now,
                expires_at=now + timedelta(seconds=self.lease_seconds),
            )
            .on_conflict_do_update(
                index_elements=[ConversationExecution.conversation_id],
                set_={
                    "owner_token": owner_token,
                    "cancel_requested": False,
                    "acquired_at": now,
                    "expires_at": now + timedelta(seconds=self.lease_seconds),
                },
                where=ConversationExecution.expires_at <= now,
            )
            .returning(ConversationExecution.owner_token)
        )
        async with self.sessions() as session:
            acquired = await session.scalar(statement)
            await session.commit()
        if acquired != owner_token:
            return False
        async with self._owners_lock:
            self._owners[conversation_id] = owner_token
        return True

    async def release(self, conversation_id: uuid.UUID) -> None:
        async with self._owners_lock:
            owner_token = self._owners.pop(conversation_id, None)
        if owner_token is None:
            return
        async with self.sessions() as session:
            await session.execute(
                delete(ConversationExecution).where(
                    ConversationExecution.conversation_id == conversation_id,
                    ConversationExecution.owner_token == owner_token,
                )
            )
            await session.commit()

    async def request_cancel(self, conversation_id: uuid.UUID) -> bool:
        now = datetime.now(UTC)
        async with self.sessions() as session:
            result = await session.execute(
                update(ConversationExecution)
                .where(
                    ConversationExecution.conversation_id == conversation_id,
                    ConversationExecution.expires_at > now,
                )
                .values(cancel_requested=True)
            )
            await session.commit()
        return bool(result.rowcount)

    async def is_cancel_requested(self, conversation_id: uuid.UUID) -> bool:
        async with self.sessions() as session:
            requested = await session.scalar(
                select(ConversationExecution.cancel_requested).where(
                    ConversationExecution.conversation_id == conversation_id,
                    ConversationExecution.expires_at > datetime.now(UTC),
                )
            )
        return bool(requested)


class DatabaseIdempotencyStore:
    """Persist completed idempotent responses across API processes."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        ttl_seconds: float,
    ) -> None:
        self.sessions = sessions
        self.ttl_seconds = ttl_seconds

    async def get(
        self,
        scope: str,
        resource_id: str,
        key: str,
    ) -> tuple[str, Any] | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope == scope,
                    IdempotencyRecord.resource_id == resource_id,
                    IdempotencyRecord.idempotency_key == key,
                    IdempotencyRecord.expires_at > datetime.now(UTC),
                )
            )
        return (row.fingerprint, row.value) if row else None

    async def put(
        self,
        scope: str,
        resource_id: str,
        key: str,
        fingerprint: str,
        value: Any,
    ) -> None:
        if hasattr(value, "model_dump"):
            stored_value = value.model_dump(mode="json")
        elif isinstance(value, tuple):
            stored_value = list(value)
        else:
            stored_value = value
        now = datetime.now(UTC)
        statement = (
            postgres_insert(IdempotencyRecord)
            .values(
                id=uuid.uuid4(),
                scope=scope,
                resource_id=resource_id,
                idempotency_key=key,
                fingerprint=fingerprint,
                value=stored_value,
                created_at=now,
                expires_at=now + timedelta(seconds=self.ttl_seconds),
            )
            .on_conflict_do_update(
                constraint="uq_idempotency_scope_resource_key",
                set_={
                    "fingerprint": fingerprint,
                    "value": stored_value,
                    "created_at": now,
                    "expires_at": now + timedelta(seconds=self.ttl_seconds),
                },
            )
        )
        async with self.sessions() as session:
            await session.execute(statement)
            await session.commit()

    async def delete_resource(self, resource_id: str) -> None:
        """Delete idempotency responses when their conversation is removed."""
        async with self.sessions() as session:
            await session.execute(
                delete(IdempotencyRecord).where(
                    IdempotencyRecord.resource_id == resource_id
                )
            )
            await session.commit()


class DatabaseRunStore:
    """Persist tool execution and lightweight model usage records."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def record_completed(
        self,
        *,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        model: str,
        prompt: str,
        response: str,
        duration_ms: int,
        tool_events: list[Any],
    ) -> None:
        now = datetime.now(UTC)
        rows: list[Any] = [
            UsageRecord(
                id=uuid.uuid4(),
                conversation_id=conversation_id,
                message_id=message_id,
                model=model,
                duration_ms=duration_ms,
                input_characters=len(prompt),
                output_characters=len(response),
                created_at=now,
            )
        ]
        for event in tool_events:
            rows.append(
                ToolExecution(
                    id=uuid.uuid4(),
                    conversation_id=conversation_id,
                    message_id=message_id,
                    tool_call_id=getattr(event, "tool_call_id", None),
                    name=getattr(event, "name", "tool"),
                    arguments=dict(getattr(event, "args", {})),
                    output=getattr(event, "output", None),
                    status="completed",
                    created_at=now,
                    completed_at=now,
                )
            )
        async with self.sessions() as session:
            session.add_all(rows)
            await session.commit()
