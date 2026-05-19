"""Bot and Dispatcher factory."""

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import settings

# Bot instance with MarkdownV2 as default parse mode
bot = Bot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
)

# Dispatcher with FSM memory storage
# For production with multiple workers, switch to RedisStorage
dp = Dispatcher(storage=MemoryStorage())
