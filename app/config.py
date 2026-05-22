"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Central configuration — reads from .env or environment."""

    # Telegram
    bot_token: str = Field(..., alias="BOT_TOKEN")
    webhook_url: str = Field("", alias="WEBHOOK_URL")
    use_polling: bool = Field(True, alias="USE_POLLING")

    # AI
    groq_api_key: str = Field("", alias="GROQ_API_KEY")
    gemini_api_key: str = Field("", alias="GEMINI_API_KEY")

    # Database
    database_url: str = Field("sqlite+aiosqlite:///./bot.db", alias="DATABASE_URL")

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    # Redis
    redis_url: str = Field("redis://localhost:6379/0", alias="REDIS_URL")

    # Logging
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # Timezone
    timezone: str = "Africa/Addis_Ababa"

    # Rate Limiting
    throttle_rate: float = 0.5  # min seconds between messages per user
    ai_requests_per_minute: int = 30

    # Render keep-alive (auto-provided by Render for web services)
    render_external_url: str = Field("", alias="RENDER_EXTERNAL_URL")

    # Reminder defaults (hours before deadline)
    reminder_offsets_hours: list[int] = [48, 24, 0]

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
