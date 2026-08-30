"""Command-line retention cleanup for expired PostgreSQL conversations."""

import argparse
import asyncio

from src.config import Settings
from src.db.checkpoint import PostgresCheckpointManager
from src.db.repositories import (
    DatabaseConversationStore,
    DatabaseIdempotencyStore,
    PostgresConversationExecutionRegistry,
)
from src.db.session import DatabaseManager
from src.services.retention_service import cleanup_expired_conversations


async def cleanup(limit: int) -> int:
    """Run one cleanup batch and return an exit status."""
    settings = Settings()
    if not settings.database_url or not settings.postgres_checkpoint_url:
        print("DATABASE_URLとCHECKPOINT_DATABASE_URLを設定してください")
        return 2

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
        checkpointer = await checkpoints.open(setup=False)
        store = DatabaseConversationStore(database.sessions)
        registry = PostgresConversationExecutionRegistry(
            database.sessions,
            lease_seconds=settings.execution_lease_seconds,
        )
        idempotency = DatabaseIdempotencyStore(
            database.sessions,
            ttl_seconds=settings.idempotency_ttl_seconds,
        )
        result = await cleanup_expired_conversations(
            store,
            registry,
            checkpointer,
            idempotency_store=idempotency,
            limit=limit,
        )
        print(
            f"削除: {result.deleted}, 実行中のため保留: "
            f"{result.skipped_busy}, 失敗: {result.failed}"
        )
        return 1 if result.failed else 0
    finally:
        await checkpoints.close()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="期限切れ会話を削除します")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 10_000:
        parser.error("--limitは1から10000の範囲で指定してください")
    raise SystemExit(asyncio.run(cleanup(args.limit)))


if __name__ == "__main__":
    main()
