"""Structured logging middleware for all Telegram updates."""

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, CallbackQuery

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
            thread_id = None
            callback_data = None

            if event.message:
                user_id = event.message.from_user.id if event.message.from_user else None
                chat_id = event.message.chat.id
                thread_id = event.message.message_thread_id
            elif event.callback_query:
                user_id = event.callback_query.from_user.id
                callback_data = event.callback_query.data
                if event.callback_query.message:
                    chat_id = event.callback_query.message.chat.id

            logger.info(
                "update_received",
                update_id=event.update_id,
                update_type=update_type,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                callback_data=callback_data,
            )

        return await handler(event, data)


class CallbackTraceMiddleware(BaseMiddleware):
    """Trace callback routing and FSM state before/after handlers."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        state = data.get("state")
        before_state = await state.get_state() if state else None
        chat_id = event.message.chat.id if event.message else None

        logger.info(
            "callback_received",
            callback_data=event.data,
            user_id=event.from_user.id,
            chat_id=chat_id,
            fsm_state_before=before_state,
        )

        try:
            result = await handler(event, data)
            after_state = await state.get_state() if state else None
            logger.info(
                "callback_handled",
                callback_data=event.data,
                user_id=event.from_user.id,
                chat_id=chat_id,
                fsm_state_before=before_state,
                fsm_state_after=after_state,
            )
            return result
        except Exception as exc:
            logger.exception(
                "callback_failed",
                callback_data=event.data,
                user_id=event.from_user.id,
                chat_id=chat_id,
                fsm_state_before=before_state,
                error_type=type(exc).__name__,
            )
            raise
