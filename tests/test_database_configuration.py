import pytest
from pydantic import ValidationError

from app.config import Settings


def test_development_allows_sqlite_for_local_work():
    settings = Settings(
        _env_file=None,
        BOT_TOKEN="test-token",
        APP_ENV="development",
        DATABASE_URL="sqlite+aiosqlite:///./bot.db",
    )

    assert settings.database_url.startswith("sqlite+")


def test_production_rejects_local_sqlite_database():
    with pytest.raises(ValidationError, match="hosted PostgreSQL"):
        Settings(
            _env_file=None,
            BOT_TOKEN="test-token",
            APP_ENV="production",
            DATABASE_URL="sqlite+aiosqlite:///./bot.db",
        )


def test_production_accepts_hosted_postgresql():
    settings = Settings(
        _env_file=None,
        BOT_TOKEN="test-token",
        APP_ENV="production",
        DATABASE_URL="postgresql+asyncpg://user:pass@example.test/academic",
    )

    assert settings.async_database_url.startswith("postgresql+asyncpg://")
