"""Tests for phase 3 persistence services."""

import uuid

import pytest

from src.db.models import Base
from src.db.repositories import DatabaseConversationStore, DatabaseNoteStore
from src.db.session import DatabaseManager
from src.services import (
    ConversationExecutionRegistry,
    IdempotencyStore,
    InMemoryConversationStore,
    InMemoryNoteStore,
)
from src.services.note_tools import create_database_note_tools
from src.services.retention_service import cleanup_expired_conversations


@pytest.mark.asyncio
async def test_database_conversation_and_note_repositories():
    database = DatabaseManager("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    user_id = uuid.uuid4()
    conversations = DatabaseConversationStore(database.sessions)
    notes = DatabaseNoteStore(database.sessions)
    try:
        await conversations.ensure_user(user_id, "test-user")
        conversation = await conversations.create(
            user_id=user_id,
            title="永続化テスト",
            retention_days=30,
        )
        fetched = await conversations.get(conversation.id, user_id=user_id)
        listed, total = await conversations.list(user_id=user_id)
        updated = await conversations.update(
            conversation.id,
            user_id=user_id,
            title="更新済み",
            status="archived",
        )
        note = await notes.create(
            conversation.id,
            user_id=user_id,
            title="メモ",
            content="内容",
        )
        updated_note = await notes.update(
            conversation.id,
            note.id,
            user_id=user_id,
            content="更新内容",
        )

        assert fetched is not None
        assert total == 1
        assert listed[0].id == conversation.id
        assert updated is not None and updated.status == "archived"
        assert updated_note is not None and updated_note.content == "更新内容"
        assert await notes.delete(
            conversation.id,
            note.id,
            user_id=user_id,
        )
        assert await conversations.delete(conversation.id, user_id=user_id)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_database_note_tools_share_conversation_store():
    user_id = uuid.uuid4()
    conversations = InMemoryConversationStore()
    notes = InMemoryNoteStore()
    conversation = await conversations.create(user_id=user_id)
    save_note, read_notes = create_database_note_tools(
        conversations,
        notes,
    )
    config = {
        "configurable": {
            "thread_id": conversation.thread_id,
            "user_id": str(user_id),
        }
    }

    saved = await save_note.ainvoke(
        {"title": "共有メモ", "content": "APIと同じ保存先"},
        config=config,
    )
    listed = await read_notes.ainvoke({}, config=config)

    assert "メモを保存しました" in saved
    assert "共有メモ" in listed
    assert "APIと同じ保存先" in listed


@pytest.mark.asyncio
async def test_retention_cleanup_deletes_checkpoint_and_metadata():
    class FakeCheckpointer:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def adelete_thread(self, thread_id: str) -> None:
            self.deleted.append(thread_id)

    store = InMemoryConversationStore()
    registry = ConversationExecutionRegistry()
    expired = await store.create(retention_days=-1)
    checkpointer = FakeCheckpointer()

    result = await cleanup_expired_conversations(
        store,
        registry,
        checkpointer,
    )

    assert result.deleted == 1
    assert result.failed == 0
    assert checkpointer.deleted == [expired.thread_id]
    assert await store.get(expired.id) is None


@pytest.mark.asyncio
async def test_idempotency_cache_can_delete_one_conversation():
    store = IdempotencyStore(ttl_seconds=60, max_entries=10)
    await store.put("message", "conversation-a", "key", "hash", {"ok": True})
    await store.put("message", "conversation-b", "key", "hash", {"ok": True})

    await store.delete_resource("conversation-a")

    assert await store.get("message", "conversation-a", "key") is None
    assert await store.get("message", "conversation-b", "key") is not None
