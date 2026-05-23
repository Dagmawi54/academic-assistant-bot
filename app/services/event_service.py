"""Events service — dashboard queries for the academic items, reminders, and duplicates."""

from typing import Sequence
from sqlalchemy import select, and_, or_, desc, asc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import AcademicItem, Reminder, DuplicateLog


async def get_upcoming_events(session: AsyncSession, group_id: int) -> Sequence[AcademicItem]:
    """Get chronological upcoming assignments, exams, and quizzes."""
    stmt = (
        select(AcademicItem)
        .options(selectinload(AcademicItem.course))
        .where(
            and_(
                AcademicItem.group_id == group_id,
                AcademicItem.item_type.in_(("assignment", "exam", "quiz")),
                AcademicItem.status.in_(("new", "active", "verified"))
            )
        )
        .order_by(asc(AcademicItem.deadline))
        .limit(20)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_scheduled_reminders(session: AsyncSession, group_id: int) -> Sequence[Reminder]:
    """Get pending reminder jobs for a group."""
    stmt = (
        select(Reminder)
        .options(
            selectinload(Reminder.academic_item).selectinload(AcademicItem.course)
        )
        .join(AcademicItem, AcademicItem.id == Reminder.item_id)
        .where(
            and_(
                AcademicItem.group_id == group_id,
                Reminder.sent == False,
                Reminder.cancelled == False
            )
        )
        .order_by(asc(Reminder.send_time))
        .limit(20)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_low_confidence_items(session: AsyncSession, group_id: int) -> Sequence[AcademicItem]:
    """Get items needing admin review (status: new) or low confidence."""
    stmt = (
        select(AcademicItem)
        .options(selectinload(AcademicItem.course))
        .where(
            and_(
                AcademicItem.group_id == group_id,
                AcademicItem.status == "new"
            )
        )
        .order_by(desc(AcademicItem.created_at))
        .limit(20)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_suppressed_duplicates(session: AsyncSession, group_id: int) -> Sequence[DuplicateLog]:
    """Get logged suppressions with reasons."""
    stmt = (
        select(DuplicateLog)
        .where(DuplicateLog.group_id == group_id)
        .order_by(desc(DuplicateLog.created_at))
        .limit(20)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_exam_coverages(session: AsyncSession, group_id: int) -> Sequence[AcademicItem]:
    """Get structured coverage data."""
    stmt = (
        select(AcademicItem)
        .options(selectinload(AcademicItem.course))
        .where(
            and_(
                AcademicItem.group_id == group_id,
                AcademicItem.item_type.in_(("exam_coverage", "coverage")),
                AcademicItem.status.in_(("new", "active", "verified"))
            )
        )
        .order_by(desc(AcademicItem.created_at))
        .limit(20)
    )
    result = await session.execute(stmt)
    return result.scalars().all()
