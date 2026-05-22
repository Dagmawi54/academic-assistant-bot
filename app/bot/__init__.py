"""Bot and Dispatcher factory."""

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import settings

# Bot instance without global MarkdownV2 (prevents plain-text crashes)
bot = Bot(
    token=settings.bot_token,
)

# Dispatcher with FSM memory storage
# For production with multiple workers, switch to RedisStorage
dp = Dispatcher(storage=MemoryStorage())
