"""Generic CRUD helpers for database operations."""

from typing import Any, Sequence, Type, TypeVar

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AcademicItem,
    AuditLog,
    Base,
    Course,
    Group,
    Reminder,
    Topic,
    User,
)

T = TypeVar("T", bound=Base)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


async def create(session: AsyncSession, obj: T) -> T:
    """Insert a new record and return it with its generated ID."""
    session.add(obj)
    await session.flush()
    await session.refresh(obj)
    return obj


async def get_by_id(session: AsyncSession, model: Type[T], record_id: int) -> T | None:
    """Fetch a single record by primary key."""
    return await session.get(model, record_id)


async def get_all(session: AsyncSession, model: Type[T], **filters: Any) -> Sequence[T]:
    """Fetch all records matching keyword filters."""
    stmt = select(model).filter_by(**filters)
    result = await session.execute(stmt)
    return result.scalars().all()


async def update_fields(
    session: AsyncSession, model: Type[T], record_id: int, **values: Any
) -> None:
    """Update specific fields on a record by ID."""
    stmt = update(model).where(model.id == record_id).values(**values)
    await session.execute(stmt)


async def delete(session: AsyncSession, obj: T) -> None:
    """Delete a record."""
    await session.delete(obj)
    await session.flush()


# ---------------------------------------------------------------------------
# Group helpers
# ---------------------------------------------------------------------------


async def get_group_by_chat_id(session: AsyncSession, chat_id: int) -> Group | None:
    stmt = select(Group).where(Group.chat_id == chat_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_group(session: AsyncSession, group_id: int) -> Group | None:
    return await get_by_id(session, Group, group_id)


async def get_managed_groups(session: AsyncSession, telegram_user_id: int) -> list[Group]:
    """Get all groups where the user has dept_admin or owner role."""
    stmt = (
        select(Group)
        .join(User)
        .where(
            User.telegram_user_id == telegram_user_id,
            User.role.in_(["owner", "dept_admin", "creator", "administrator"]),
            Group.active == True,  # noqa: E712
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Topic helpers
# ---------------------------------------------------------------------------


async def get_topic(session: AsyncSession, chat_id: int, thread_id: int) -> Topic | None:
    stmt = select(Topic).where(
        Topic.chat_id == chat_id,
        Topic.message_thread_id == thread_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_active_topics(session: AsyncSession, group_id: int) -> Sequence[Topic]:
    stmt = select(Topic).where(
        Topic.group_id == group_id,
        Topic.status == "active",
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_general_topic(session: AsyncSession, group_id: int) -> Topic | None:
    stmt = select(Topic).where(
        Topic.group_id == group_id,
        Topic.topic_type == "general",
        Topic.status == "active",
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Course helpers
# ---------------------------------------------------------------------------


async def get_course_by_name(
    session: AsyncSession, group_id: int, course_name: str
) -> Course | None:
    stmt = select(Course).where(
        Course.group_id == group_id,
        Course.course_name == course_name,
        Course.active == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_active_courses(session: AsyncSession, group_id: int) -> Sequence[Course]:
    stmt = select(Course).where(
        Course.group_id == group_id,
        Course.active == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Reminder helpers
# ---------------------------------------------------------------------------


async def get_pending_reminders(session: AsyncSession) -> Sequence[Reminder]:
    """Get all unsent, non-cancelled reminders."""
    stmt = select(Reminder).where(
        Reminder.sent == False,  # noqa: E712
        Reminder.cancelled == False,  # noqa: E712
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def cancel_item_reminders(session: AsyncSession, item_id: int) -> None:
    """Cancel all pending reminders for a given academic item."""
    stmt = (
        update(Reminder)
        .where(
            Reminder.item_id == item_id,
            Reminder.sent == False,  # noqa: E712
        )
        .values(cancelled=True)
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------


async def get_user(session: AsyncSession, telegram_user_id: int, group_id: int) -> User | None:
    stmt = select(User).where(
        User.telegram_user_id == telegram_user_id,
        User.group_id == group_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_any_group(session: AsyncSession, telegram_user_id: int) -> Sequence[User]:
    stmt = select(User).where(User.telegram_user_id == telegram_user_id)
    result = await session.execute(stmt)
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


async def log_action(
    session: AsyncSession,
    *,
    action: str,
    telegram_user_id: int | None = None,
    chat_id: int | None = None,
    details: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        action=action,
        details=details,
    )
    session.add(entry)
    await session.flush()
    return entry
