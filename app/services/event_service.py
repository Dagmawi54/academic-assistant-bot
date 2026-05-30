"""Events service — dashboard queries for the academic items, reminders, and duplicates."""

from typing import Sequence
from sqlalchemy import select, and_, desc, asc, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import AcademicItem, Course, DuplicateLog, Reminder, Topic


async def get_upcoming_events(
    session: AsyncSession,
    group_id: int,
    *,
    item_types: Sequence[str] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> Sequence[AcademicItem]:
    """Get chronological upcoming assignments, exams, and quizzes."""
    stmt = (
        select(AcademicItem)
        .options(selectinload(AcademicItem.course).selectinload(Course.topic))
        .where(
            and_(
                AcademicItem.group_id == group_id,
                AcademicItem.item_type.in_(tuple(item_types or ("assignment", "exam", "quiz"))),
                AcademicItem.status.in_(("new", "active", "verified"))
            )
        )
        .order_by(asc(AcademicItem.deadline))
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_recent_items(
    session: AsyncSession,
    group_id: int,
    *,
    limit: int = 20,
    offset: int = 0,
) -> Sequence[AcademicItem]:
    """Get recently detected academic items for observability."""
    stmt = (
        select(AcademicItem)
        .options(selectinload(AcademicItem.course).selectinload(Course.topic))
        .where(AcademicItem.group_id == group_id)
        .order_by(desc(AcademicItem.created_at))
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_item_detail(session: AsyncSession, group_id: int, item_id: int) -> AcademicItem | None:
    """Fetch one AcademicItem with course/topic/reminders loaded."""
    stmt = (
        select(AcademicItem)
        .options(
            selectinload(AcademicItem.course).selectinload(Course.topic),
            selectinload(AcademicItem.reminders),
        )
        .where(AcademicItem.group_id == group_id, AcademicItem.id == item_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_reminder_counts(session: AsyncSession, item_ids: Sequence[int]) -> dict[int, int]:
    """Return pending reminder counts keyed by AcademicItem id."""
    if not item_ids:
        return {}
    stmt = (
        select(Reminder.item_id, func.count(Reminder.id))
        .where(
            Reminder.item_id.in_(item_ids),
            Reminder.sent == False,
            Reminder.cancelled == False,
        )
        .group_by(Reminder.item_id)
    )
    result = await session.execute(stmt)
    return {int(item_id): int(count) for item_id, count in result.all()}


async def get_item_for_review(session: AsyncSession, group_id: int, item_id: int) -> AcademicItem | None:
    """Fetch one reviewable academic item scoped to a managed group."""
    stmt = (
        select(AcademicItem)
        .options(selectinload(AcademicItem.course).selectinload(Course.topic))
        .where(AcademicItem.group_id == group_id, AcademicItem.id == item_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_coverage_item(session: AsyncSession, group_id: int, item_id: int) -> AcademicItem | None:
    """Fetch one coverage item scoped to a managed group."""
    stmt = (
        select(AcademicItem)
        .options(selectinload(AcademicItem.course).selectinload(Course.topic))
        .where(
            AcademicItem.group_id == group_id,
            AcademicItem.id == item_id,
            AcademicItem.item_type.in_(("exam_coverage", "coverage")),
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_scheduled_reminders(session: AsyncSession, group_id: int) -> Sequence[Reminder]:
    """Get pending reminder jobs for a group."""
    stmt = (
        select(Reminder)
        .options(
            selectinload(Reminder.academic_item).selectinload(AcademicItem.course)
            .selectinload(Course.topic)
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
        .options(selectinload(AcademicItem.course).selectinload(Course.topic))
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
        .options(selectinload(AcademicItem.course).selectinload(Course.topic))
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


async def get_duplicate_detail(
    session: AsyncSession,
    group_id: int,
    duplicate_id: int,
) -> DuplicateLog | None:
    """Fetch a single duplicate suppression record scoped to a group."""
    stmt = select(DuplicateLog).where(
        DuplicateLog.group_id == group_id,
        DuplicateLog.id == duplicate_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def get_scheduler_jobs() -> list[dict[str, str | int | None]]:
    """Return currently registered APScheduler jobs for dashboard visibility."""
    from app.reminders.scheduler import scheduler

    jobs = []
    for job in scheduler.get_jobs():
        reminder_id = None
        if job.id.startswith("reminder_"):
            try:
                reminder_id = int(job.id.removeprefix("reminder_"))
            except ValueError:
                reminder_id = None

        jobs.append(
            {
                "job_id": job.id,
                "reminder_id": reminder_id,
                "next_run_time": (
                    job.next_run_time.isoformat()
                    if getattr(job, "next_run_time", None)
                    else None
                ),
            }
        )
    return jobs


async def get_scheduler_job_details(session: AsyncSession, group_id: int) -> list[dict[str, str | int | None]]:
    """Return APScheduler jobs enriched with academic item/course metadata where possible."""
    jobs = get_scheduler_jobs()
    details: list[dict[str, str | int | None]] = []
    for job in jobs:
        reminder_id = job.get("reminder_id")
        row = dict(job)
        if reminder_id:
            reminder = await session.get(Reminder, reminder_id)
            item = None
            if reminder:
                item = await session.get(AcademicItem, reminder.item_id)
            if item and item.group_id == group_id:
                course = await session.get(Course, item.course_id) if item.course_id else None
                topic = await session.get(Topic, course.topic_id) if course and course.topic_id else None
                row.update(
                    {
                        "course": course.course_name if course else None,
                        "topic": topic.topic_name if topic else None,
                        "reminder_type": item.item_type,
                    }
                )
            elif item:
                continue
        details.append(row)
    return details
