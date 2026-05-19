"""Routing service — orchestrates classification, AI fallback, routing, and item creation."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import crud
from app.database.models import AcademicItem
from app.routing.classifier import classify, ClassificationResult
from app.routing.router import resolve_destination
from app.ai.confidence import should_auto_approve
from app.ai.groq_client import groq_client
from app.ai.prompts import build_extraction_prompt
from app.ai.extraction import parse_extraction
from app.events.bus import emit, ASSIGNMENT_DETECTED, EXAM_DETECTED
from app.bot import bot
from app.reminders.formatter import format_academic_notification
from app.utils.text import escape_md
from app.logging import get_logger

logger = get_logger("routing_service")


async def process_group_message(
    session: AsyncSession,
    *,
    chat_id: int,
    thread_id: int | None,
    text: str,
    user_id: int | None,
    message_id: int,
) -> None:
    """Full message processing pipeline: classify → extract → route → notify."""

    # 1. Ensure group is registered
    group = await crud.get_group_by_chat_id(session, chat_id)
    if not group:
        return  # Unregistered group — ignore

    # 2. Rule-based classification
    classification = classify(text)
    logger.info(
        "classified",
        chat_id=chat_id,
        msg_type=classification.message_type,
        confidence=classification.confidence,
        course=classification.course_hint,
    )

    # Skip non-academic messages
    if classification.message_type in ("DISCUSSION", "UNKNOWN"):
        if classification.confidence > 0.5:
            return  # Clearly not academic

        # Low confidence — try AI if available
        ai_result = await _try_ai_extraction(text)
        if ai_result and ai_result.get("type") not in (
            "discussion",
            "unknown",
            "DISCUSSION",
            "UNKNOWN",
        ):
            classification = _merge_ai_result(classification, ai_result)
        else:
            return

    # 3. If rule-based confidence is low, augment with AI
    if classification.confidence < 0.7 and classification.message_type != "DISCUSSION":
        ai_result = await _try_ai_extraction(text)
        if ai_result:
            classification = _merge_ai_result(classification, ai_result)

    # 4. Resolve destination topic
    destination = await resolve_destination(session, group.id, classification)

    # 5. Create academic item if it has a deadline or structured content
    item = None
    if (
        classification.message_type in ("ASSIGNMENT", "EXAM", "EXAM_COVERAGE")
        and classification.deadline
    ):
        course = None
        if classification.course_hint:
            course = await crud.get_course_by_name(session, group.id, classification.course_hint)

        # Duplicate detection check
        from app.services.academic_item_service import is_semantic_duplicate
        from app.metrics.tracker import tracker

        await tracker.record_duplicate_check(suppressed=False)
        duplicate = await is_semantic_duplicate(
            session,
            group.id,
            course.id if course else None,
            classification.message_type.lower(),
            classification.deadline,
        )
        if duplicate:
            await tracker.record_duplicate_check(suppressed=True)
            logger.info("skipped_duplicate_item", duplicate_id=duplicate.id)
            return

        item = AcademicItem(
            group_id=group.id,
            course_id=course.id if course else None,
            item_type=classification.message_type.lower(),
            title=classification.title,
            deadline=classification.deadline,
            room=classification.room,
            coverage=classification.coverage,
            status="active" if should_auto_approve(classification.confidence) else "new",
            confidence=classification.confidence,
            source_message_id=message_id,
            source_chat_id=chat_id,
            raw_text=text,
        )
        item = await crud.create(session, item)
        logger.info("item_created", item_id=item.id, item_type=item.item_type)

    # 6. Send notification to destination topic (if found and auto-approved)
    if destination and should_auto_approve(classification.confidence):
        notification = format_academic_notification(classification, item)
        try:
            await bot.send_message(
                chat_id=destination.chat_id,
                message_thread_id=destination.message_thread_id,
                text=notification,
            )
            logger.info("routed", destination_topic=destination.topic_name)
        except Exception:
            logger.exception("routing_send_failed", topic_id=destination.id)

    # 7. Emit events for reminder creation
    if item:
        event = ASSIGNMENT_DETECTED if item.item_type == "assignment" else EXAM_DETECTED
        await emit(event, item_id=item.id)


async def _try_ai_extraction(text: str) -> dict | None:
    """Attempt AI extraction, returns None on failure. Falls back to Gemini."""
    from app.metrics.tracker import tracker

    try:
        # Try Groq first
        messages = build_extraction_prompt(text)
        result = await groq_client.complete(
            messages,
            response_format={"type": "json_object"},
        )
        if result:
            parsed = parse_extraction(result)
            if parsed:
                if "confidence" in parsed:
                    await tracker.record_ai_extraction(parsed["confidence"])
                return parsed

        # Fall back to Gemini if Groq fails or parses empty
        from app.ai.gemini_client import gemini_client

        if gemini_client.is_configured:
            logger.info("groq_unsuccessful_falling_back_to_gemini")
            await tracker.record_fallback_usage()
            sys_prompt = build_extraction_prompt(text)[0]["content"]
            user_prompt = build_extraction_prompt(text)[1]["content"]
            gemini_result = await gemini_client.complete(sys_prompt, user_prompt)
            if gemini_result:
                parsed = parse_extraction(gemini_result)
                if parsed and "confidence" in parsed:
                    await tracker.record_ai_extraction(parsed["confidence"])
                return parsed

    except Exception:
        logger.exception("ai_extraction_failed")
    return None


def _merge_ai_result(rule_result: ClassificationResult, ai_data: dict) -> ClassificationResult:
    """Merge AI extraction data into the rule-based classification."""
    from app.routing.classifier import ClassificationResult as CR

    return CR(
        message_type=ai_data.get("type", rule_result.message_type),
        confidence=max(rule_result.confidence, ai_data.get("confidence", 0.0)),
        course_hint=ai_data.get("course") or rule_result.course_hint,
        deadline=ai_data.get("deadline") or rule_result.deadline,
        room=ai_data.get("room") or rule_result.room,
        coverage=ai_data.get("coverage") or rule_result.coverage,
        title=ai_data.get("title") or rule_result.title,
    )
