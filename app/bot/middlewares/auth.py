"""Authorization middleware for admin-only endpoints."""

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import crud
from app.logging import get_logger

logger = get_logger("auth")

ADMIN_ROLES = {"owner", "dept_admin", "section_admin"}


class AdminAuthMiddleware(BaseMiddleware):
    """Blocks non-admin users from admin-only handlers.

    Usage: apply to specific routers, not globally.
    """

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        if not event.from_user:
            return None

        session: AsyncSession | None = data.get("session")
        if session is None:
            logger.warning("auth_no_session")
            return None

        # Check if user has admin role in any group
        users = await crud.get_user_any_group(session, event.from_user.id)
        is_admin = any(u.role in ADMIN_ROLES for u in users)

        if not is_admin:
            logger.info("auth_denied", user_id=event.from_user.id)
            return None  # Silently ignore

        data["is_admin"] = True
        return await handler(event, data)
