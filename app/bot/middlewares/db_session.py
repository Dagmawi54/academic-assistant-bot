"""Middleware that injects an async DB session into every handler."""

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.database.session import async_session_factory


class DbSessionMiddleware(BaseMiddleware):
    """Provides a fresh async DB session via data['session']."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with async_session_factory() as session:
            async with session.begin():
                data["session"] = session
                result = await handler(event, data)
                # Commit happens automatically if no exception
            return result
