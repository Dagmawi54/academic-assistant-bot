"""Structured logging middleware for all Telegram updates."""

import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import crud
from app.database import session as db_session_module
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
        import uuid
        trace_id = str(uuid.uuid4())
        data["trace_id"] = trace_id

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
                trace_id=trace_id,
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
        if not isinstance(event, CallbackQuery) and not (
            hasattr(event, "data") and hasattr(event, "from_user")
        ):
            return await handler(event, data)

        state = data.get("state")
        before_state = await state.get_state() if state else None
        chat_id = event.message.chat.id if event.message else None
        ref_id = _callback_ref_id()

        await _persist_callback_trace(
            data,
            phase="button_pressed",
            ref_id=ref_id,
            callback_data=event.data,
            user_id=event.from_user.id,
            chat_id=chat_id,
            fsm_state_before=before_state,
        )
        await _persist_callback_trace(
            data,
            phase="callback_received",
            ref_id=ref_id,
            callback_data=event.data,
            user_id=event.from_user.id,
            chat_id=chat_id,
            fsm_state_before=before_state,
        )

        logger.info(
            "callback_received",
            callback_data=event.data,
            user_id=event.from_user.id,
            chat_id=chat_id,
            fsm_state_before=before_state,
        )

        try:
            await _persist_callback_trace(
                data,
                phase="callback_started",
                ref_id=ref_id,
                callback_data=event.data,
                user_id=event.from_user.id,
                chat_id=chat_id,
                fsm_state_before=before_state,
            )
            result = await handler(event, data)
            after_state = await state.get_state() if state else None
            logger.info(
                "callback_completed",
                callback_data=event.data,
                user_id=event.from_user.id,
                chat_id=chat_id,
                fsm_state_before=before_state,
                fsm_state_after=after_state,
            )
            await _persist_callback_trace(
                data,
                phase="callback_completed",
                ref_id=ref_id,
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
            await _persist_callback_trace(
                data,
                phase="callback_failed",
                ref_id=ref_id,
                callback_data=event.data,
                user_id=event.from_user.id,
                chat_id=chat_id,
                fsm_state_before=before_state,
                error_type=type(exc).__name__,
                error=str(exc)[:300],
            )
            await _answer_callback_failure(event, ref_id)
            return None


def _callback_ref_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"CB-{stamp}"


async def _answer_callback_failure(event: CallbackQuery, ref_id: str) -> None:
    try:
        await event.answer(f"Action failed. Reference ID: {ref_id}", show_alert=True)
    except Exception:
        logger.exception("callback_failure_answer_failed", ref_id=ref_id)
    if not event.message:
        return
    try:
        await event.message.edit_text(
            f"❌ <b>Action failed</b>\n\nReference:\n<code>{ref_id}</code>",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("callback_failure_edit_failed", ref_id=ref_id)


async def _persist_callback_trace(data: dict[str, Any], **payload: Any) -> None:
    details = json.dumps(payload, default=str, sort_keys=True)
    existing_session = data.get("session")
    if isinstance(existing_session, AsyncSession):
        await crud.log_action(
            existing_session,
            action="callback_trace",
            telegram_user_id=payload.get("user_id"),
            chat_id=payload.get("chat_id"),
            details=details,
        )
        await existing_session.flush()
        return

    session_obj = db_session_module.async_session_factory()
    if isinstance(session_obj, AsyncSession):
        await crud.log_action(
            session_obj,
            action="callback_trace",
            telegram_user_id=payload.get("user_id"),
            chat_id=payload.get("chat_id"),
            details=details,
        )
        await session_obj.flush()
        return

    async with session_obj as session:
        async with session.begin():
            await crud.log_action(
                session,
                action="callback_trace",
                telegram_user_id=payload.get("user_id"),
                chat_id=payload.get("chat_id"),
                details=details,
            )
