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


async def get_user_role(session: AsyncSession, telegram_user_id: int, group_id: int) -> str:
    """Get a user's role in a specific group. Defaults to 'student'."""
    user = await crud.get_user(session, telegram_user_id, group_id)
    return user.role if user else "student"


async def is_admin(session: AsyncSession, telegram_user_id: int, group_id: int) -> bool:
    """Check if user has admin privileges in the group."""
    role = await get_user_role(session, telegram_user_id, group_id)
    return role in ADMIN_ROLES


async def is_moderator(session: AsyncSession, telegram_user_id: int, group_id: int) -> bool:
    """Check if user has moderator or higher privileges."""
    role = await get_user_role(session, telegram_user_id, group_id)
    return role in MODERATOR_ROLES


async def has_role(
    session: AsyncSession,
    telegram_user_id: int,
    group_id: int,
    required_role: str,
) -> bool:
    """Check if user's role meets or exceeds the required role level."""
    actual = await get_user_role(session, telegram_user_id, group_id)
    return ROLE_HIERARCHY.get(actual, 0) >= ROLE_HIERARCHY.get(required_role, 0)


async def is_admin_in_any_group(session: AsyncSession, telegram_user_id: int) -> bool:
    """Check if user is admin in any registered group (for DM menus)."""
    users = await crud.get_user_any_group(session, telegram_user_id)
    return any(u.role in ADMIN_ROLES for u in users)
