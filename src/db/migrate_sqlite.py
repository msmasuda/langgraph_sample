"""Migrate legacy SQLite conversations, notes, and checkpoints to PostgreSQL."""

import argparse
import asyncio
import sqlite3
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import insert as postgres_insert

from src.agent import AsyncCompatibleSqliteSaver, DB_PATH
from src.config import Settings
from src.db.checkpoint import PostgresCheckpointManager
from src.db.models import Conversation, Note
from src.db.repositories import DatabaseConversationStore
from src.db.session import DatabaseManager
from src.tools import NOTES_DB


def _conversation_id(thread_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(thread_id)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"langgraph-thread:{thread_id}")


def _parse_datetime(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _source_thread_ids(checkpoint_path: Path, notes_path: Path) -> set[str]:
    thread_ids: set[str] = set()
    if checkpoint_path.is_file():
        with sqlite3.connect(checkpoint_path) as connection:
            thread_ids.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints"
                )
            )
    if notes_path.is_file():
        with sqlite3.connect(notes_path) as connection:
            thread_ids.update(
                str(row[0])
                for row in connection.execute("SELECT DISTINCT thread_id FROM notes")
            )
    return thread_ids


async def _migrate_metadata(
    database: DatabaseManager,
    settings: Settings,
    thread_ids: set[str],
    notes_path: Path,
) -> tuple[int, int]:
    now = datetime.now(UTC)
    conversations = [
        {
            "id": _conversation_id(thread_id),
            "user_id": settings.default_user_id,
            "thread_id": thread_id,
            "title": f"移行済み会話: {thread_id[:40]}",
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "expires_at": now
            + timedelta(days=settings.conversation_retention_days),
        }
        for thread_id in sorted(thread_ids)
    ]
    note_rows: list[dict[str, Any]] = []
    if notes_path.is_file():
        with sqlite3.connect(notes_path) as connection:
            for legacy_id, thread_id, title, content, created_at in connection.execute(
                "SELECT id, thread_id, title, content, created_at FROM notes ORDER BY id"
            ):
                created = _parse_datetime(created_at, now)
                note_rows.append(
                    {
                        "id": uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"legacy-note:{thread_id}:{legacy_id}",
                        ),
                        "user_id": settings.default_user_id,
                        "conversation_id": _conversation_id(str(thread_id)),
                        "title": str(title),
                        "content": str(content),
                        "created_at": created,
                        "updated_at": created,
                    }
                )

    async with database.sessions() as session:
        if conversations:
            await session.execute(
                postgres_insert(Conversation)
                .values(conversations)
                .on_conflict_do_nothing(index_elements=[Conversation.id])
            )
        if note_rows:
            await session.execute(
                postgres_insert(Note)
                .values(note_rows)
                .on_conflict_do_nothing(index_elements=[Note.id])
            )
        await session.commit()
    return len(conversations), len(note_rows)


async def _migrate_checkpoints(
    checkpoint_path: Path,
    target: Any,
    thread_ids: set[str],
) -> int:
    if not checkpoint_path.is_file():
        return 0
    connection = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    source = AsyncCompatibleSqliteSaver(connection)
    migrated = 0
    try:
        for thread_id in sorted(thread_ids):
            config = {"configurable": {"thread_id": thread_id}}
            checkpoints = await asyncio.to_thread(lambda: list(source.list(config)))
            for item in reversed(checkpoints):
                namespace = item.config.get("configurable", {}).get(
                    "checkpoint_ns", ""
                )
                parent = item.parent_config or {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": namespace,
                    }
                }
                saved_config = await target.aput(
                    parent,
                    item.checkpoint,
                    item.metadata,
                    item.checkpoint.get("channel_versions", {}),
                )
                pending_by_task: dict[str, list[tuple[str, Any]]] = defaultdict(list)
                for task_id, channel, value in item.pending_writes or []:
                    pending_by_task[task_id].append((channel, value))
                for task_id, writes in pending_by_task.items():
                    await target.aput_writes(saved_config, writes, task_id)
                migrated += 1
    finally:
        connection.close()
    return migrated


async def migrate(*, dry_run: bool) -> int:
    settings = Settings()
    if not settings.database_url or not settings.postgres_checkpoint_url:
        print("DATABASE_URLとCHECKPOINT_DATABASE_URLを設定してください")
        return 2
    thread_ids = _source_thread_ids(DB_PATH, NOTES_DB)
    print(f"移行対象の会話: {len(thread_ids)}件")
    if dry_run:
        return 0

    database = DatabaseManager(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    checkpoints = PostgresCheckpointManager(
        settings.postgres_checkpoint_url,
        pool_size=settings.database_pool_size,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        conversation_store = DatabaseConversationStore(database.sessions)
        await conversation_store.ensure_user(
            settings.default_user_id,
            settings.default_user_subject,
        )
        conversation_count, note_count = await _migrate_metadata(
            database,
            settings,
            thread_ids,
            NOTES_DB,
        )
        target = await checkpoints.open(setup=True)
        checkpoint_count = await _migrate_checkpoints(DB_PATH, target, thread_ids)
        print(
            f"移行完了: 会話 {conversation_count}件, メモ {note_count}件, "
            f"チェックポイント {checkpoint_count}件"
        )
        return 0
    finally:
        await checkpoints.close()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLiteデータをPostgreSQLへ移行")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="実際に移行する（省略時は対象件数の確認のみ）",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(migrate(dry_run=not args.apply)))


if __name__ == "__main__":
    main()
