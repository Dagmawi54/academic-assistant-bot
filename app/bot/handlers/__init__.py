"""Handler registration hub."""

from aiogram import Dispatcher

from app.bot.handlers.commands import router as commands_router
from app.bot.handlers.group import router as group_router
from app.bot.handlers.admin import router as admin_router
from app.bot.handlers.events import router as events_router
from app.bot.handlers.communications import router as communications_router
from app.bot.handlers.dm import router as dm_router


def register_all_handlers(dp: Dispatcher) -> None:
    """Register all handler routers on the dispatcher.

    Order matters:
    1. Commands (highest priority — explicit /commands)
    2. Admin (DM callbacks and FSM flows)
    3. Group (catch-all for group messages)
    """
    dp.include_router(commands_router)
    dp.include_router(admin_router)
    dp.include_router(events_router)
    dp.include_router(communications_router)
    dp.include_router(dm_router)
    dp.include_router(group_router)
