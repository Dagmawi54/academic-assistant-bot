"""Role-based permission checks and decorators."""

from functools import wraps
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import crud
from app.logging import get_logger

logger = get_logger("permissions")

# Role hierarchy (higher index = more privilege)
ROLE_HIERARCHY = {
    "student": 0,
    "moderator": 1,
    "representative": 2,
    "section_admin": 3,
    "dept_admin": 4,
    "owner": 5,
}

ADMIN_ROLES = {"owner", "dept_admin", "section_admin"}
MODERATOR_ROLES = ADMIN_ROLES | {"moderator", "representative"}


async def get_user_role(session: AsyncSession, telegram_user_id: int, chat_id: int) -> str:
    """Get a user's role in a specific group by chat_id. Defaults to 'student'."""
    group = await crud.get_group_by_chat_id(session, chat_id)
    if not group:
        return "student"
    user = await crud.get_user(session, telegram_user_id, group.id)
    return user.role if user else "student"


async def is_admin(session: AsyncSession, telegram_user_id: int, chat_id: int) -> bool:
    """Check if user has admin privileges in the group."""
    role = await get_user_role(session, telegram_user_id, chat_id)
    return role in ADMIN_ROLES


async def is_moderator(session: AsyncSession, telegram_user_id: int, chat_id: int) -> bool:
    """Check if user has moderator or higher privileges."""
    role = await get_user_role(session, telegram_user_id, chat_id)
    return role in MODERATOR_ROLES


async def has_role(
    session: AsyncSession,
    telegram_user_id: int,
    chat_id: int,
    required_role: str,
) -> bool:
    """Check if user's role meets or exceeds the required role level."""
    actual = await get_user_role(session, telegram_user_id, chat_id)
    return ROLE_HIERARCHY.get(actual, 0) >= ROLE_HIERARCHY.get(required_role, 0)


async def is_admin_in_any_group(session: AsyncSession, telegram_user_id: int) -> bool:
    """Check if user is admin in any registered group (for DM menus)."""
    users = await crud.get_user_any_group(session, telegram_user_id)
    return any(u.role in ADMIN_ROLES for u in users)


def require_role(allowed_roles: list[str]) -> Callable:
    """Decorator that restricts a group command handler to specific roles.
    
    Usage:
        @router.message(Command("review"))
        @require_role(["creator", "administrator", "dept_admin"])
        async def cmd_review(message, session): ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(message: Any, *args: Any, **kwargs: Any) -> Any:
            session = kwargs.get("session")
            if session is None:
                for arg in args:
                    if isinstance(arg, AsyncSession):
                        session = arg
                        break

            if session is None or message.from_user is None:
                return

            # Check Telegram's native admin status
            chat_member = await message.chat.get_member(message.from_user.id)
            tg_status = chat_member.status if chat_member else None

            if tg_status in allowed_roles:
                return await func(message, *args, **kwargs)

            # Check our internal role system
            user_role = await get_user_role(session, message.from_user.id, message.chat.id)
            if user_role in allowed_roles:
                return await func(message, *args, **kwargs)

            await message.answer("❌ You don't have permission for this action.")
        return wrapper
    return decorator
