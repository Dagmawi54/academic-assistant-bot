"""Routing service — orchestrates classification, AI fallback, routing, and item creation."""

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import crud
from app.database.models import AcademicItem
from app.routing.classifier import classify, ClassificationResult
from app.routing.router import resolve_destination
from app.ai.confidence import should_auto_approve
from app.ai.academic_extraction_client import academic_extraction_client
from app.ai.prompts import build_extraction_prompt
from app.ai.extraction import parse_extraction
from app.events.bus import emit, ASSIGNMENT_DETECTED, EXAM_DETECTED
from app.bot import bot
from app.reminders.formatter import format_academic_notification
from app.logging import get_logger
from app.services.topic_context import resolve_topic_context

logger = get_logger("routing_service")

# Academic types that should always create an AcademicItem
ACADEMIC_TYPES = {"ASSIGNMENT", "EXAM", "QUIZ", "EXAM_COVERAGE", "SCHEDULE_UPDATE", "GENERAL_EVENT", "COURSE_EVENT"}


async def process_group_message(
    session: AsyncSession,
    *,
    chat_id: int,
    thread_id: int | None,
    text: str,
    user_id: int | None,
    message_id: int,
    trace_id: str | None = None,
) -> None:
    """Full message processing pipeline: classify → extract → route → notify."""
    logger.info(
        "ROUTE_ENTRY",
        trace_id=trace_id,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        message_id=message_id,
        text_length=len(text),
    )

    # 1. Ensure group is registered
    group = await crud.get_group_by_chat_id(session, chat_id)
    if group:
        await _log_event_pipeline_snapshot(
            session,
            phase="message_processed",
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            message_id=message_id,
            group_id=group.id,
        )
    if not group:
        logger.info("ROUTE_EXIT", trace_id=trace_id, handler_name="process_group_message")
        return  # Unregistered group — ignore

    topic_context = await resolve_topic_context(
        session,
        group_id=group.id,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    logger.info(
        "academic_topic_context",
        chat_id=chat_id,
        thread_id=thread_id,
        topic_id=topic_context.topic.id if topic_context.topic else None,
        topic_name=topic_context.topic.topic_name if topic_context.topic else None,
        course_id=topic_context.course.id if topic_context.course else None,
        course_name=topic_context.course_name,
    )

    # 2. Rule-based classification
    classification = classify(text)
    if not classification.course_hint and topic_context.course_name:
        classification.course_hint = topic_context.course_name
        if not classification.title:
            classification.title = _build_contextual_title(
                classification.message_type,
                topic_context.course_name,
            )

    logger.info(
        "academic_classified",
        chat_id=chat_id,
        thread_id=thread_id,
        msg_type=classification.message_type,
        confidence=classification.confidence,
        course=classification.course_hint,
        deadline=str(classification.deadline) if classification.deadline else None,
    )

    # Structured trace for observability
    logger.info(
        "GROUP_ROUTE",
        trace_id=trace_id,
        confidence=classification.confidence,
        decision=classification.message_type,
        course=classification.course_hint,
        has_deadline=bool(classification.deadline),
        has_room=bool(classification.room),
    )

    # Skip non-academic messages (DISCUSSION or UNKNOWN)
    if classification.message_type in ("DISCUSSION", "UNKNOWN"):
        if len(text.split()) < 3:
            logger.info("ROUTE_EXIT", trace_id=trace_id, handler_name="process_group_message")
            return

        # Dual threshold routing logic
        if classification.confidence >= 0.7:
            # High confidence it is just discussion -> Drop
            logger.info("ROUTE_EXIT", trace_id=trace_id, handler_name="process_group_message")
            return
        elif classification.confidence >= 0.5:
            # Mid confidence -> Trigger classifier review / AI trial
            logger.info("ROUTE_LOG", trace_id=trace_id, action="classifier_review_triggered")
            ai_result = await _try_ai_extraction(text)
        else:
            # Low confidence -> Fallback to AI
            logger.info("ROUTE_LOG", trace_id=trace_id, action="ai_fallback_triggered")
            ai_result = await _try_ai_extraction(text)

        if ai_result and ai_result.get("type") not in (
            "discussion",
            "unknown",
            "DISCUSSION",
            "UNKNOWN",
        ):
            classification = _merge_ai_result(classification, ai_result)
        else:
            logger.info("ROUTE_EXIT", trace_id=trace_id, handler_name="process_group_message")
            return

    # 3. If rule-based confidence is low, augment with AI
    if classification.confidence < 0.7 and classification.message_type != "DISCUSSION":
        ai_result = await _try_ai_extraction(text)
        if ai_result:
            classification = _merge_ai_result(classification, ai_result)
            logger.info(
                "academic_ai_extraction_merged",
                chat_id=chat_id,
                thread_id=thread_id,
                ai_type=ai_result.get("type"),
                ai_confidence=ai_result.get("confidence"),
            )

    # 4. Resolve destination topic
    destination = await resolve_destination(session, group.id, classification)

    # 5. Create academic item for ANY academic type (not just those with deadlines)
    item = None
    if classification.message_type in ACADEMIC_TYPES:
        course = topic_context.course
        if not course and classification.course_hint:
            course = await crud.get_course_by_name(session, group.id, classification.course_hint)

        # Duplicate detection check
        from app.services.academic_item_service import is_semantic_duplicate
        from app.metrics.tracker import tracker

        await tracker.record_duplicate_check(suppressed=False)
        logger.info(
            "academic_duplicate_check_started",
            group_id=group.id,
            course_id=course.id if course else None,
            item_type=classification.message_type.lower(),
            deadline=str(classification.deadline) if classification.deadline else None,
        )
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
            logger.info(
                "academic_duplicate_suppressed",
                duplicate_id=duplicate.id,
                source_message_id=message_id,
            )

            from app.database.models import DuplicateLog
            duplicate_log = DuplicateLog(
                group_id=group.id,
                existing_item_id=duplicate.id,
                source_message_id=message_id,
                reason="Semantic duplicate (similar title within 48h)",
                raw_text=text
            )
            await crud.create(session, duplicate_log)
            logger.info("ROUTE_EXIT", trace_id=trace_id, handler_name="process_group_message")
            return
        logger.info("academic_duplicate_check_clear", source_message_id=message_id)

        chat_link = None
        if chat_id and message_id:
            # construct telegram internal link if supergroup
            base_chat_id = str(chat_id).replace("-100", "")
            if thread_id:
                chat_link = f"https://t.me/c/{base_chat_id}/{thread_id}/{message_id}"
            else:
                chat_link = f"https://t.me/c/{base_chat_id}/{message_id}"

        coverage_value = classification.coverage
        if classification.message_type == "EXAM_COVERAGE":
            from app.services.coverage_parser import dump_coverage, parse_coverage_text

            coverage_value = dump_coverage(parse_coverage_text(text))

        item = AcademicItem(
            group_id=group.id,
            course_id=course.id if course else None,
            item_type=classification.message_type.lower(),
            title=classification.title,
            deadline=classification.deadline,
            room=classification.room,
            coverage=coverage_value,
            status="active" if should_auto_approve(classification.confidence) else "new",
            confidence=classification.confidence,
            source_message_id=message_id,
            source_chat_id=chat_id,
            source_message_link=chat_link,
            raw_text=text,
        )
        item = await crud.create(session, item)
        await _log_event_pipeline_snapshot(
            session,
            phase="item_created",
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            message_id=message_id,
            group_id=group.id,
            item_id=item.id,
            item_type=item.item_type,
            course_id=item.course_id,
            deadline=str(item.deadline) if item.deadline else None,
        )
        logger.info(
            "academic_item_created",
            item_id=item.id,
            item_type=item.item_type,
            course_id=item.course_id,
            topic_id=topic_context.topic.id if topic_context.topic else None,
            deadline=str(item.deadline) if item.deadline else None,
            confidence=item.confidence,
        )

        if item.deadline:
            from app.services.reminder_service import create_reminders_for_item_in_session

            reminders = await create_reminders_for_item_in_session(session, item)
            await _log_event_pipeline_snapshot(
                session,
                phase="reminders_created",
                chat_id=chat_id,
                thread_id=thread_id,
                user_id=user_id,
                message_id=message_id,
                group_id=group.id,
                item_id=item.id,
                reminder_count=len(reminders),
                reminder_ids=[reminder.id for reminder in reminders],
            )
            logger.info("academic_reminders_created", item_id=item.id, reminder_count=len(reminders))
            logger.info(
                "academic_scheduler_jobs_registered",
                item_id=item.id,
                job_ids=[f"reminder_{reminder.id}" for reminder in reminders],
                next_runs=[str(reminder.send_time) for reminder in reminders],
            )

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
            logger.info(
                "academic_ack_sent",
                item_id=item.id,
                chat_id=chat_id,
                thread_id=thread_id,
            )
            await _log_event_pipeline_snapshot(
                session,
                phase="ack_sent",
                chat_id=chat_id,
                thread_id=thread_id,
                user_id=user_id,
                message_id=message_id,
                group_id=group.id,
                item_id=item.id,
            )
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
        logger.info("academic_event_emitted", item_id=item.id, event_name=event)
        await emit(event, item_id=item.id, reminders_already_created=True)
    logger.info(
        "ROUTE_EXIT",
        trace_id=trace_id,
        handler_name="process_group_message",
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        item_id=item.id if item else None,
        item_type=item.item_type if item else None,
    )


def _build_contextual_title(message_type: str, course_name: str) -> str | None:
    labels = {
        "ASSIGNMENT": "Assignment",
        "EXAM": "Exam",
        "QUIZ": "Quiz",
        "EXAM_COVERAGE": "Exam Coverage",
        "SCHEDULE_UPDATE": "Schedule Update",
        "GENERAL_EVENT": "Notice",
        "COURSE_EVENT": "Course Event",
    }
    label = labels.get(message_type)
    return f"{course_name} - {label}" if label else None


def _build_detection_feedback(classification: ClassificationResult, item: AcademicItem) -> str:
    """Build a visible detection feedback message for the source topic."""
    when = ""
    if classification.deadline:
        from app.utils.timezone import to_addis

        when = f" for {to_addis(classification.deadline).strftime('%A')}"

    if classification.message_type == "ASSIGNMENT":
        return "📌 <b>Assignment deadline recorded.</b>\nReminders scheduled." if item.deadline else "📌 <b>Assignment recorded.</b>"
    if classification.message_type == "QUIZ":
        return f"📌 <b>Quiz added{when}.</b>\nReminders scheduled." if item.deadline else "📌 <b>Quiz added.</b>"
    if classification.message_type == "EXAM":
        return f"📌 <b>Exam added{when}.</b>\nReminders scheduled." if item.deadline else "📌 <b>Exam added.</b>"
    if classification.message_type == "EXAM_COVERAGE":
        return "📌 <b>Coverage updated.</b>"
    if classification.message_type == "SCHEDULE_UPDATE":
        return "📌 <b>Schedule update recorded.</b>"
    return "📌 <b>Academic item recorded.</b>"




async def _try_ai_extraction(text: str) -> dict | None:
    """Attempt AI extraction, returns None on failure. Falls back to Gemini."""
    from app.metrics.tracker import tracker

    try:
        logger.info("academic_ai_extraction_attempted", provider="groq", text_length=len(text))
        messages = build_extraction_prompt(text)
        result = await academic_extraction_client.complete_json(
            messages,
            response_format={"type": "json_object"},
        )
        if result:
            parsed = parse_extraction(result)
            if parsed:
                logger.info(
                    "academic_ai_extraction_result",
                    ai_type=parsed.get("type"),
                    ai_confidence=parsed.get("confidence"),
                    ai_course=parsed.get("course"),
                )
                if "confidence" in parsed:
                    await tracker.record_ai_extraction(parsed["confidence"])
                return parsed
        await tracker.record_fallback_usage()

    except Exception:
        logger.exception("ai_extraction_failed")
    return None


async def _log_event_pipeline_snapshot(session: AsyncSession, **payload) -> None:
    await crud.log_action(
        session,
        action="event_pipeline_snapshot",
        telegram_user_id=payload.get("user_id"),
        chat_id=payload.get("chat_id"),
        details=json.dumps(payload, default=str, sort_keys=True),
    )


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
