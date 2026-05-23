"""Reminder service — create, cancel, rebuild reminders from academic items."""

from app.database import crud
from app.database.models import AcademicItem, Course, Reminder, Topic
from app.database.session import async_session_factory
from app.config import settings
from app.utils.timezone import reminder_times
from app.logging import get_logger

logger = get_logger("reminder_service")


async def create_reminders_for_item(item_id: int) -> None:
    """Create reminder entries for an academic item's deadline."""
    async with async_session_factory() as session:
        async with session.begin():
            item = await crud.get_by_id(session, AcademicItem, item_id)
            await create_reminders_for_item_in_session(session, item)


async def create_reminders_for_item_in_session(
    session,
    item: AcademicItem | None,
) -> list[Reminder]:
    """Create reminder entries using the caller's active transaction."""
    if not item or not item.deadline:
        logger.info(
            "academic_reminder_rows_created",
            item_id=item.id if item else None,
            count=0,
            reason="missing_item_or_deadline",
        )
        return []

    thread_id = None
    if item.course_id:
        course = await crud.get_by_id(session, Course, item.course_id)
        if course and course.topic_id:
            topic = await crud.get_by_id(session, Topic, course.topic_id)
            thread_id = topic.message_thread_id if topic else None

    times = reminder_times(item.deadline, settings.reminder_offsets_hours)
    reminders = [
        Reminder(
            item_id=item.id,
            chat_id=item.source_chat_id or 0,
            thread_id=thread_id,
            send_time=send_time,
        )
        for send_time in times
    ]

    for reminder in reminders:
        session.add(reminder)

    await session.flush()

    from app.reminders.scheduler import schedule_reminder

    for reminder in reminders:
        schedule_reminder(reminder.id, reminder.send_time)

    logger.info(
        "academic_reminder_rows_created",
        item_id=item.id,
        count=len(reminders),
        thread_id=thread_id,
    )
    return reminders


async def recreate_reminders_for_item(item_id: int) -> None:
    """Cancel old reminders and create new ones (for updated items)."""
    async with async_session_factory() as session:
        async with session.begin():
            await crud.cancel_item_reminders(session, item_id)
    await create_reminders_for_item(item_id)


async def cancel_group_reminders(group_id: int) -> None:
    """Cancel all pending reminders for a group (semester close)."""
    async with async_session_factory() as session:
        async with session.begin():
            # Get all active items for this group
            items = await crud.get_all(session, AcademicItem, group_id=group_id)
            cancelled = 0
            for item in items:
                if item.status in ("new", "active", "verified"):
                    await crud.cancel_item_reminders(session, item.id)
                    cancelled += 1

            logger.info(
                "group_reminders_cancelled",
                group_id=group_id,
                items_affected=cancelled,
            )
