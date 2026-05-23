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

# Academic types that should always create an AcademicItem
ACADEMIC_TYPES = {"ASSIGNMENT", "EXAM", "EXAM_COVERAGE", "SCHEDULE_UPDATE", "GENERAL_EVENT", "COURSE_EVENT"}


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
        deadline=str(classification.deadline) if classification.deadline else None,
    )

    # Skip non-academic messages (DISCUSSION or UNKNOWN)
    if classification.message_type in ("DISCUSSION", "UNKNOWN"):
        # If the rule-based engine is reasonably sure it's just chat,
        # or if the message is very short, do NOT call the expensive AI.
        if classification.confidence >= 0.5 or len(text.split()) < 3:
            return  # Clearly not academic or too short to be meaningful

        # Only trial AI for high-importance messages that might have been missed
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

    # 5. Create academic item for ANY academic type (not just those with deadlines)
    item = None
    if classification.message_type in ACADEMIC_TYPES:
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
            title=classification.title,
        )
        if duplicate:
            await tracker.record_duplicate_check(suppressed=True)
            logger.info("skipped_duplicate_item", duplicate_id=duplicate.id)

            from app.database.models import DuplicateLog
            duplicate_log = DuplicateLog(
                group_id=group.id,
                existing_item_id=duplicate.id,
                source_message_id=message_id,
                reason="Semantic duplicate (similar title within 48h)",
                raw_text=text
            )
            await crud.create(session, duplicate_log)
            return

        chat_link = None
        if chat_id and message_id:
            # construct telegram internal link if supergroup
            base_chat_id = str(chat_id).replace("-100", "")
            if thread_id:
                chat_link = f"https://t.me/c/{base_chat_id}/{thread_id}/{message_id}"
            else:
                chat_link = f"https://t.me/c/{base_chat_id}/{message_id}"

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
            source_message_link=chat_link,
            raw_text=text,
        )
        item = await crud.create(session, item)
        logger.info("item_created", item_id=item.id, item_type=item.item_type)

    # 6. Send detection feedback to the SOURCE topic (visible acknowledgment)
    if item:
        feedback = _build_detection_feedback(classification, item)
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=feedback,
                parse_mode="HTML",
            )
            logger.info("detection_feedback_sent", item_id=item.id)
        except Exception:
            logger.exception("detection_feedback_failed", item_id=item.id)

    # 7. Send notification to destination topic (if different from source and auto-approved)
    if item and destination and should_auto_approve(classification.confidence):
        # Only route if destination is different from source
        source_match = (destination.chat_id == chat_id and destination.message_thread_id == thread_id)
        if not source_match:
            notification = format_academic_notification(classification, item)
            try:
                await bot.send_message(
                    chat_id=destination.chat_id,
                    message_thread_id=destination.message_thread_id,
                    text=notification,
                    parse_mode="HTML",
                )
                logger.info("routed", destination_topic=destination.topic_name)
            except Exception:
                logger.exception("routing_send_failed", topic_id=destination.id)

    # 8. Emit events for reminder creation
    if item and item.deadline:
        event = ASSIGNMENT_DETECTED if item.item_type == "assignment" else EXAM_DETECTED
        await emit(event, item_id=item.id)


def _build_detection_feedback(classification: ClassificationResult, item: AcademicItem) -> str:
    """Build a visible detection feedback message for the source topic."""
    type_icons = {
        "ASSIGNMENT": "📝",
        "EXAM": "📋",
        "EXAM_COVERAGE": "📖",
        "SCHEDULE_UPDATE": "🔄",
        "GENERAL_EVENT": "📢",
        "COURSE_EVENT": "📌",
    }
    icon = type_icons.get(classification.message_type, "📌")
    type_label = classification.message_type.replace("_", " ").title()

    lines = [f"✅ <b>{icon} {type_label} Detected</b>"]

    if classification.title:
        lines.append(f"  <i>{classification.title}</i>")

    if classification.deadline:
        from app.utils.timezone import format_datetime
        lines.append(f"  📅 {format_datetime(classification.deadline)}")

    if classification.room:
        lines.append(f"  🏛 Room: {classification.room}")

    if classification.coverage:
        lines.append(f"  📖 Coverage: {classification.coverage}")

    if classification.course_hint:
        lines.append(f"  📚 Course: {classification.course_hint}")

    conf_pct = int(classification.confidence * 100)
    lines.append(f"  <code>Confidence: {conf_pct}%</code>")

    if item.deadline:
        lines.append("\n⏰ <i>Reminders will be scheduled automatically.</i>")

    return "\n".join(lines)


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
