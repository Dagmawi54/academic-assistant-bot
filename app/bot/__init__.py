"""Bot and Dispatcher factory."""

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from app.config import settings

# Bot instance without global MarkdownV2 (prevents plain-text crashes)
bot = Bot(
    token=settings.bot_token,
)

# Dispatcher with FSM Redis storage for production persistence
storage = RedisStorage.from_url(settings.redis_url)
dp = Dispatcher(storage=storage)
