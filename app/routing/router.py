"""Message routing — resolves destination topic from classification result."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import crud
from app.database.models import Topic
from app.routing.classifier import ClassificationResult
from app.cache import redis_cache
from app.logging import get_logger

logger = get_logger("router")


async def resolve_destination(
    session: AsyncSession,
    group_id: int,
    classification: ClassificationResult,
) -> Topic | None:
    """Determine which topic a classified message should be routed to.

    Returns:
        The destination Topic, or None if the message should not be routed.
    """
    msg_type = classification.message_type

    # Discussion or unknown — no routing
    if msg_type in ("DISCUSSION", "UNKNOWN"):
        return None

    # General events → general topic
    if msg_type == "GENERAL_EVENT":
        topic = await crud.get_general_topic(session, group_id)
        if not topic:
            logger.warning("no_general_topic", group_id=group_id)
        return topic

    # Course-specific events → course topic
    if classification.course_hint:
        course = await crud.get_course_by_name(session, group_id, classification.course_hint)
        if course and course.topic_id:
            topic = await crud.get_by_id(session, Topic, course.topic_id)
            if topic and topic.status == "active":
                return topic

        # Try fuzzy match by checking all courses
        courses = await crud.get_active_courses(session, group_id)
        hint_lower = classification.course_hint.lower()
        for c in courses:
            if hint_lower in c.course_name.lower() or c.course_name.lower() in hint_lower:
                if c.topic_id:
                    topic = await crud.get_by_id(session, Topic, c.topic_id)
                    if topic and topic.status == "active":
                        return topic

    # Schedule updates without specific course → general topic
    if msg_type == "SCHEDULE_UPDATE":
        return await crud.get_general_topic(session, group_id)

    # Fallback: if course-specific but no matching course found
    logger.info(
        "no_matching_course",
        group_id=group_id,
        course_hint=classification.course_hint,
        message_type=msg_type,
    )
    return None
