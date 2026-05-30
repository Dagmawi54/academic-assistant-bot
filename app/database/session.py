"""Async database session factory."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.logging import get_logger

logger = get_logger("database")

engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncSession:
    """Create a new async session (for manual use outside middleware)."""
    async with async_session_factory() as session:
        yield session


async def init_db() -> None:
    """Create all tables (dev only — use Alembic for production)."""
    from app.database.models import Base
    from sqlalchemy import text

    # First transaction for table creation
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database_tables_checked")
        
    # Lightweight compatibility migrations for existing deployments.
    optional_migrations = [
        "ALTER TABLE groups ADD COLUMN IF NOT EXISTS ai_moderation_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE academic_items ADD COLUMN IF NOT EXISTS source_message_link VARCHAR(255)",
    ]
    for statement in optional_migrations:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(statement))
        except Exception as exc:
            logger.info(
                "database_optional_migration_skipped",
                statement=statement,
                error_type=type(exc).__name__,
            )


async def close_db() -> None:
    """Dispose the engine on shutdown."""
    await engine.dispose()
