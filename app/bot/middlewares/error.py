"""Global exception handling middleware."""

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from app.logging import get_logger

logger = get_logger("error_middleware")


class GlobalErrorMiddleware(BaseMiddleware):
    """Catches unhandled exceptions in bot handlers and prevents crashes."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception as e:
            # Check if this exception is related to AI failure or something else
            error_type = type(e).__name__
            logger.exception("unhandled_bot_exception", error_type=error_type)

            # Try to notify the user if possible
            try:
                if isinstance(event, Message):
                    await event.answer(
                        "⚠️ An unexpected error occurred while processing your request. The admins have been notified."
                    )
                elif isinstance(event, CallbackQuery) and event.message:
                    await event.message.answer(
                        "⚠️ An unexpected error occurred. Please try again later."
                    )
            except Exception:
                logger.exception("failed_to_send_error_notification")

            # Rethrow if necessary or absorb safely depending on requirement
            # Usually absorbing ensures the bot keeps running, but aiogram 3
            # handles exceptions internally anyway. For safety:
            return None
