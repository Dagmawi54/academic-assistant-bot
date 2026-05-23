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
        
    # Second, isolated transaction for the alter table hack so it doesn't rollback create_all if it fails
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE groups ADD COLUMN ai_moderation_enabled BOOLEAN DEFAULT FALSE")
            )
            await conn.execute(
                text("ALTER TABLE academic_items ADD COLUMN source_message_link VARCHAR(255)")
            )
    except Exception as exc:
        # Postgres throws if column already exists
        logger.info("database_optional_migration_skipped", error_type=type(exc).__name__)


async def close_db() -> None:
    """Dispose the engine on shutdown."""
    await engine.dispose()
