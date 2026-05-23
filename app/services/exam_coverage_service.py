"""Manual exam coverage creation helpers."""

from __future__ import annotations

from app.bot import bot
from app.database import crud
from app.database.models import AcademicItem, Course, Group, Topic
from app.logging import get_logger
from app.utils.timezone import now_addis

logger = get_logger("exam_coverage_service")


def build_coverage_text(coverage_text: str, notes: str | None = None) -> str:
    coverage = coverage_text.strip()
    if notes:
        coverage = f"{coverage} | {notes.strip()}"
    return coverage


async def create_exam_coverage_entry(
    session,
    *,
    group: Group,
    course: Course,
    topic: Topic | None,
    exam_type: str,
    coverage_text: str,
    notes: str | None,
    created_by: int | None,
) -> AcademicItem:
    """Persist a manual coverage entry and post it into the course topic."""
    coverage = build_coverage_text(coverage_text, notes)
    label = exam_type.replace("_", " ").title()
    item = AcademicItem(
        group_id=group.id,
        course_id=course.id,
        item_type="exam_coverage",
        title=f"{course.course_name} {label} Coverage",
        coverage=coverage,
        status="active",
        confidence=1.0,
        source_chat_id=group.chat_id,
        raw_text=coverage,
        created_at=now_addis(),
    )
    item = await crud.create(session, item)
    await crud.log_action(
        session,
        action="exam_coverage_created",
        telegram_user_id=created_by,
        chat_id=group.chat_id,
        details=f"course_id={course.id} item_id={item.id} exam_type={exam_type}",
    )

    if topic:
        text = (
            f"📘 <b>{course.course_name} {label} Coverage</b>\n\n"
            f"{coverage_text.strip()}"
        )
        if notes:
            text += f"\n\n<i>{notes.strip()}</i>"
        await bot.send_message(
            chat_id=topic.chat_id,
            message_thread_id=topic.message_thread_id,
            text=text,
            parse_mode="HTML",
        )
        logger.info(
            "exam_coverage_posted",
            item_id=item.id,
            topic_id=topic.id,
            thread_id=topic.message_thread_id,
        )

    return item
