"""Retention cleanup for expired conversations and checkpoints."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """Summary of one bounded retention cleanup pass."""

    deleted: int
    skipped_busy: int
    failed: int


async def cleanup_expired_conversations(
    conversation_store: Any,
    execution_registry: Any,
    checkpointer: Any,
    *,
    idempotency_store: Any | None = None,
    limit: int = 100,
) -> RetentionResult:
    """Delete expired metadata and LangGraph state without racing active runs."""
    deleted = 0
    skipped_busy = 0
    failed = 0
    records = await conversation_store.list_expired(limit=limit)
    for record in records:
        if not await execution_registry.reserve(record.id):
            skipped_busy += 1
            continue
        try:
            await checkpointer.adelete_thread(record.thread_id)
            if idempotency_store is not None:
                await idempotency_store.delete_resource(str(record.id))
            removed = await conversation_store.delete(
                record.id,
                user_id=record.user_id,
            )
            if removed:
                deleted += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        finally:
            await execution_registry.release(record.id)
    return RetentionResult(
        deleted=deleted,
        skipped_busy=skipped_busy,
        failed=failed,
    )
