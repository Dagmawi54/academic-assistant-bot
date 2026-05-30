"""Resolve academic context from Telegram forum topics."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import crud
from app.database.models import Course, Topic
from app.logging import get_logger

logger = get_logger("topic_context")


@dataclass(frozen=True)
class TopicContext:
    """Academic context inferred from a Telegram forum topic."""

    topic: Topic | None
    course: Course | None

    @property
    def course_name(self) -> str | None:
        return self.course.course_name if self.course else None


async def resolve_topic_context(
    session: AsyncSession,
    *,
    group_id: int,
    chat_id: int,
    thread_id: int | None,
) -> TopicContext:
    """Resolve active topic and course mapping for an incoming Telegram message."""
    if thread_id is None:
        logger.info("topic_context_resolved", group_id=group_id, chat_id=chat_id, status="no_thread")
        return TopicContext(topic=None, course=None)

    topic = await crud.get_topic(session, chat_id, thread_id)
    if not topic:
        logger.info(
            "topic_context_resolved",
            group_id=group_id,
            chat_id=chat_id,
            thread_id=thread_id,
            status="topic_missing",
        )
        return TopicContext(topic=None, course=None)

    if topic.group_id != group_id or topic.status != "active":
        logger.info(
            "topic_context_resolved",
            group_id=group_id,
            chat_id=chat_id,
            thread_id=thread_id,
            topic_id=topic.id,
            topic_status=topic.status,
            status="topic_inactive",
        )
        return TopicContext(topic=topic, course=None)

    stmt = (
        select(Course)
        .where(
            Course.group_id == group_id,
            Course.topic_id == topic.id,
            Course.active == True,  # noqa: E712
        )
        .order_by(Course.id.desc())
    )
    result = await session.execute(stmt)
    courses = result.scalars().all()
    course = courses[0] if courses else None
    if len(courses) > 1:
        logger.warning(
            "topic_context_multiple_active_courses",
            group_id=group_id,
            topic_id=topic.id,
            selected_course_id=course.id,
            duplicate_course_ids=[c.id for c in courses[1:]],
        )

    logger.info(
        "topic_context_resolved",
        group_id=group_id,
        chat_id=chat_id,
        thread_id=thread_id,
        topic_id=topic.id,
        topic_name=topic.topic_name,
        topic_type=topic.topic_type,
        course_id=course.id if course else None,
        course_name=course.course_name if course else None,
        status="course_linked" if course else "topic_unlinked",
    )
    return TopicContext(topic=topic, course=course)
