"""Structured logging middleware for all Telegram updates."""

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.logging import get_logger

logger = get_logger("telegram")


class LoggingMiddleware(BaseMiddleware):
    """Logs every incoming Telegram update with structured metadata."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            update_type = event.event_type
            user_id = None
            chat_id = None

            if event.message:
                user_id = event.message.from_user.id if event.message.from_user else None
                chat_id = event.message.chat.id
            elif event.callback_query:
                user_id = event.callback_query.from_user.id
                if event.callback_query.message:
                    chat_id = event.callback_query.message.chat.id

            logger.info(
                "update_received",
                update_id=event.update_id,
                update_type=update_type,
                user_id=user_id,
                chat_id=chat_id,
            )

        return await handler(event, data)
