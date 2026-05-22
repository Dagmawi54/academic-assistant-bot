"""Pytest configuration — provides async SQLAlchemy session fixture."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.models import Base


# Use an in-memory SQLite database for tests
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DB_URL, echo=False)
TestSessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    """Create all tables before each test, drop after."""
    # Redirect global session factory to use our test engine
    monkeypatch.setattr("app.database.session.async_session_factory", TestSessionFactory)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)



@pytest_asyncio.fixture
async def session():
    """Provide a transactional async session for each test."""
    async with TestSessionFactory() as session:
        async with session.begin():
            yield session
