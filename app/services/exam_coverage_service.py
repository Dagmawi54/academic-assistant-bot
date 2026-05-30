"""Manual exam coverage creation helpers."""

from __future__ import annotations

from app.bot import bot
from app.database import crud
from app.database.models import AcademicItem, Course, Group, Topic
from app.logging import get_logger
from app.services.coverage_parser import dump_coverage, merge_coverage, parse_coverage_text
from app.utils.timezone import now_addis

logger = get_logger("exam_coverage_service")


def build_coverage_text(coverage_text: str, notes: str | None = None) -> str:
    coverage = coverage_text.strip()
    if notes:
        coverage = f"{coverage} | {notes.strip()}"
    return coverage


async def stitch_item_coverage(session, item_id: int, incoming_text: str | None = None) -> AcademicItem | None:
    """Merge parsed coverage data into an AcademicItem and persist it."""
    item = await crud.get_by_id(session, AcademicItem, item_id)
    if not item:
        logger.warning("coverage_stitch_missing_item", item_id=item_id)
        return None

    merged = merge_coverage(item.coverage, incoming_text or item.raw_text)
    item.coverage = dump_coverage(merged)
    item.status = "active"
    item.version = (item.version or 1) + 1
    await session.flush()
    await session.refresh(item)
    await crud.log_action(
        session,
        action="exam_coverage_stitched",
        chat_id=item.source_chat_id,
        details=f"item_id={item.id} included={len(merged['included_topics'])} excluded={len(merged['excluded_topics'])}",
    )
    logger.info(
        "coverage_stitched",
        item_id=item.id,
        included=merged["included_topics"],
        excluded=merged["excluded_topics"],
        exam_type=merged["exam_type"],
    )
    return item


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
    structured_coverage = dump_coverage(parse_coverage_text(coverage, exam_type=exam_type))
    label = exam_type.replace("_", " ").title()
    item = AcademicItem(
        group_id=group.id,
        course_id=course.id,
        item_type="exam_coverage",
        title=f"{course.course_name} {label} Coverage",
        coverage=structured_coverage,
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
