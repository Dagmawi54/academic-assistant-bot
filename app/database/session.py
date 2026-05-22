"""Async database session factory."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

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

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Safely attempt to add the new column if it's an existing DB without wiping
        try:
            await conn.execute(
                text("ALTER TABLE groups ADD COLUMN ai_moderation_enabled BOOLEAN DEFAULT 0")
            )
        except Exception:
            pass


async def close_db() -> None:
    """Dispose the engine on shutdown."""
    await engine.dispose()
