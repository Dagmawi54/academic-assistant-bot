"""Semester lifecycle service."""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import crud
from app.database.models import Course, Group, Topic
from app.events.bus import emit, SEMESTER_CLOSED
from app.logging import get_logger

logger = get_logger("semester_service")


async def close_semester(session: AsyncSession, group_id: int) -> None:
    """Close the current semester for a group.

    Steps:
    1. Close all active course topics
    2. Deactivate all current courses
    3. Cancel all pending reminders (via event)
    4. Keep group record (semester will be updated on activation)
    """
    # Close topics
    from app.bot import bot
    topics = await crud.get_active_topics(session, group_id)
    for topic in topics:
        if topic.topic_type == "course":
            await crud.update_fields(session, Topic, topic.id, status="closed")
            if topic.message_thread_id and topic.message_thread_id > 0:
                try:
                    await bot.edit_forum_topic(
                        chat_id=topic.chat_id,
                        message_thread_id=topic.message_thread_id,
                        name=f"[CLOSED] {topic.topic_name}"
                    )
                    await bot.close_forum_topic(
                        chat_id=topic.chat_id,
                        message_thread_id=topic.message_thread_id
                    )
                except Exception as e:
                    logger.warning("failed_closing_telegram_topic", error=str(e), topic_id=topic.id)

    # Deactivate courses
    courses = await crud.get_active_courses(session, group_id)
    for course in courses:
        await crud.update_fields(session, Course, course.id, active=False)

    logger.info(
        "semester_closed",
        group_id=group_id,
        topics_closed=len([t for t in topics if t.topic_type == "course"]),
        courses_deactivated=len(courses),
    )

    # Emit event to cancel reminders
    await emit(SEMESTER_CLOSED, group_id=group_id)


async def activate_semester(session: AsyncSession, group_id: int, new_semester: int) -> None:
    """Activate a new semester for a group.

    Steps:
    1. Update group semester number
    2. New courses and topics should be added via admin menu
    """
    await crud.update_fields(session, Group, group_id, semester=new_semester)

    logger.info(
        "semester_activated",
        group_id=group_id,
        new_semester=new_semester,
    )
