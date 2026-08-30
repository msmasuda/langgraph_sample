"""LangGraph PostgreSQL checkpointer lifecycle."""

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class PostgresCheckpointManager:
    """Own a pooled, strict-deserialization PostgreSQL checkpointer."""

    def __init__(
        self,
        database_url: str,
        *,
        pool_size: int = 5,
        connect_timeout_seconds: float = 10.0,
    ) -> None:
        self.pool = AsyncConnectionPool(
            conninfo=database_url,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            min_size=1,
            max_size=pool_size,
            open=False,
            timeout=connect_timeout_seconds,
        )
        serializer = JsonPlusSerializer(allowed_msgpack_modules=None)
        self.checkpointer = AsyncPostgresSaver(self.pool, serde=serializer)

    async def open(self, *, setup: bool = True) -> AsyncPostgresSaver:
        """Open the pool and optionally initialize LangGraph-owned tables."""
        await self.pool.open(wait=True)
        if setup:
            await self.checkpointer.setup()
        return self.checkpointer

    async def close(self) -> None:
        """Close all checkpointer connections."""
        await self.pool.close()
