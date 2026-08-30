"""Async SQLAlchemy engine and session lifecycle."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class DatabaseManager:
    """Own the application database engine and session factory."""

    def __init__(
        self,
        database_url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        connect_timeout_seconds: float = 10.0,
    ) -> None:
        engine_options: dict[str, object] = {
            "pool_pre_ping": True,
        }
        if database_url.startswith("postgresql"):
            engine_options.update(
                pool_size=pool_size,
                max_overflow=max_overflow,
                connect_args={"timeout": connect_timeout_seconds},
            )
        self.engine: AsyncEngine = create_async_engine(database_url, **engine_options)
        self.sessions = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def ping(self) -> bool:
        """Return whether a trivial database query succeeds."""
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    async def close(self) -> None:
        """Dispose all pooled connections."""
        await self.engine.dispose()
