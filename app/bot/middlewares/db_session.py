"""Middleware that injects an async DB session into every handler."""

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.database.session import async_session_factory
from app.logging import get_logger

logger = get_logger("db_session")


class DbSessionMiddleware(BaseMiddleware):
    """Provides a fresh async DB session via data['session']."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with async_session_factory() as session:
            tx_context = _event_context(event)
            logger.info("db_session_opened", **tx_context)
            try:
                async with session.begin():
                    data["session"] = session
                    result = await handler(event, data)
                logger.info("db_session_committed", **tx_context)
                return result
            except Exception as exc:
                logger.exception(
                    "db_session_rollback",
                    error_type=type(exc).__name__,
                    **tx_context,
                )
                raise


def _event_context(event: TelegramObject) -> dict[str, Any]:
    if isinstance(event, Message):
        return {
            "event_type": "message",
            "chat_id": event.chat.id,
            "thread_id": event.message_thread_id,
            "user_id": event.from_user.id if event.from_user else None,
        }
    if isinstance(event, CallbackQuery):
        return {
            "event_type": "callback_query",
            "chat_id": event.message.chat.id if event.message else None,
            "thread_id": None,
            "user_id": event.from_user.id,
            "callback_data": event.data,
        }
    return {"event_type": type(event).__name__}
