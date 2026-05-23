"""Bot and Dispatcher factory."""

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

from app.config import settings
from app.logging import get_logger

logger = get_logger("bot")

# Bot instance without global MarkdownV2 (prevents plain-text crashes)
bot = Bot(
    token=settings.bot_token,
)

# Dispatcher with FSM Redis storage for production persistence
storage = RedisStorage.from_url(settings.redis_url)
dp = Dispatcher(storage=storage)


async def validate_fsm_storage() -> bool:
    """Validate the Redis-backed FSM storage used by aiogram."""
    redis = getattr(storage, "redis", None)
    if redis is None:
        logger.error("fsm_storage_invalid", storage_type=type(storage).__name__)
        return False

    try:
        await redis.ping()
        logger.info("fsm_storage_connected", storage_type=type(storage).__name__)
        return True
    except Exception as exc:
        logger.error(
            "fsm_storage_unavailable",
            storage_type=type(storage).__name__,
            error_type=type(exc).__name__,
        )
        return False
