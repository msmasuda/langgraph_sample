"""FastAPI lifespan wiring for the PostgreSQL production runtime."""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI

from src.config import Settings
from src.db import (
    DatabaseConversationStore,
    DatabaseIdempotencyStore,
    DatabaseManager,
    DatabaseNoteStore,
    DatabaseRunStore,
    PostgresCheckpointManager,
    PostgresConversationExecutionRegistry,
)
from src.services import AgentService
from src.services.note_tools import create_database_note_tools
from src.tools import calculator, get_current_datetime, web_search


@asynccontextmanager
async def database_lifespan(
    app: FastAPI,
    settings: Settings,
    *,
    enabled: bool,
) -> AsyncIterator[None]:
    """Initialize and close PostgreSQL resources owned by the API process."""
    database: DatabaseManager | None = None
    checkpoints: PostgresCheckpointManager | None = None

    try:
        if enabled:
            if not settings.database_url or not settings.postgres_checkpoint_url:
                raise RuntimeError(
                    "DATABASE_URLとCHECKPOINT_DATABASE_URLを設定してください"
                )
            database = DatabaseManager(
                settings.database_url,
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_max_overflow,
                connect_timeout_seconds=settings.database_connect_timeout_seconds,
            )
            if not await database.ping():
                raise RuntimeError("PostgreSQLに接続できません")

            conversation_store = DatabaseConversationStore(database.sessions)
            note_store = DatabaseNoteStore(database.sessions)
            await conversation_store.ensure_user(
                settings.default_user_id,
                settings.default_user_subject,
            )

            checkpoints = PostgresCheckpointManager(
                settings.postgres_checkpoint_url,
                pool_size=settings.database_pool_size,
                connect_timeout_seconds=settings.database_connect_timeout_seconds,
            )
            checkpointer = await checkpoints.open(setup=True)
            note_tools = create_database_note_tools(
                conversation_store,
                note_store,
            )
            agent_service = AgentService.create(
                settings=settings,
                checkpointer=checkpointer,
                tools=[get_current_datetime, calculator, web_search, *note_tools],
            )

            app.state.database_manager = database
            app.state.checkpoint_manager = checkpoints
            app.state.conversation_store = conversation_store
            app.state.note_store = note_store
            app.state.execution_registry = PostgresConversationExecutionRegistry(
                database.sessions,
                lease_seconds=settings.execution_lease_seconds,
            )
            app.state.idempotency_store = DatabaseIdempotencyStore(
                database.sessions,
                ttl_seconds=settings.idempotency_ttl_seconds,
            )
            app.state.run_store = DatabaseRunStore(database.sessions)
            app.state.agent_service = agent_service

        yield
    finally:
        if checkpoints is not None:
            await checkpoints.close()
        if database is not None:
            await database.close()


def build_lifespan(settings: Settings, *, enabled: bool) -> Any:
    """Return a FastAPI-compatible lifespan callable."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with database_lifespan(app, settings, enabled=enabled):
            yield

    return lifespan
