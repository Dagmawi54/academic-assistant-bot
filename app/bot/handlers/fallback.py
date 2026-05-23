"""Fallback handlers for unmatched Telegram interactions."""

from aiogram import Router, types

from app.logging import get_logger

logger = get_logger("fallback_handlers")

router = Router(name="fallback")


@router.callback_query()
async def cb_unmatched(callback: types.CallbackQuery) -> None:
    """Answer unmatched callbacks so inline buttons never spin silently."""
    logger.warning(
        "callback_unmatched",
        callback_data=callback.data,
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id if callback.message else None,
    )
    await callback.answer("This action is not available in the current state.", show_alert=True)
