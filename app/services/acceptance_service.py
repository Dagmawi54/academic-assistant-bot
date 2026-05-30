"""Runtime acceptance and diagnostics helpers backed by AuditLog."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import crud
from app.database.models import AcademicItem, AuditLog, DuplicateLog, Reminder


ACCEPTANCE_FEATURES: tuple[str, ...] = (
    "Exam Detection",
    "Assignment Detection",
    "Quiz Detection",
    "Coverage Detection",
    "Coverage Stitch",
    "Coverage Edit",
    "Events Dashboard",
    "Scheduler",
    "Reminders",
    "Announcements",
    "Broadcast",
    "Analytics",
    "Audit Logs",
    "Reply PDF",
    "Reply DOCX",
    "Reply Image",
    "Reply Voice",
    "Group Conversation Memory",
    "Ask RAG",
)

FEATURE_SLUGS = {
    feature: feature.lower().replace("/", "").replace(" ", "_")
    for feature in ACCEPTANCE_FEATURES
}
FEATURE_BY_SLUG = {slug: feature for feature, slug in FEATURE_SLUGS.items()}


async def record_acceptance_result(
    session: AsyncSession,
    *,
    feature_name: str,
    result: str,
    tester: int | None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Store a manual runtime acceptance result in AuditLog."""
    normalized = result.strip().lower()
    action = "acceptance_passed" if normalized == "passed" else "acceptance_failed"
    payload = {
        "feature_name": feature_name,
        "result": normalized,
        "tester": tester,
        "timestamp": _now_iso(),
        "reason": reason or "",
        "metadata": metadata or {},
    }
    return await crud.log_action(
        session,
        action=action,
        telegram_user_id=tester,
        details=json.dumps(payload, sort_keys=True),
    )


async def get_acceptance_records(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Return the latest acceptance record for every known feature."""
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.action.in_(("acceptance_passed", "acceptance_failed")))
        .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
    )
    records: dict[str, dict[str, Any]] = {}
    for row in result.scalars().all():
        payload = _json_details(row.details)
        feature_name = payload.get("feature_name")
        if feature_name in ACCEPTANCE_FEATURES and feature_name not in records:
            records[feature_name] = {
                **payload,
                "created_at": row.created_at,
                "action": row.action,
            }
    return records


async def render_acceptance_dashboard(session: AsyncSession) -> str:
    """Render the admin acceptance dashboard as Telegram HTML."""
    records = await get_acceptance_records(session)
    lines = ["<b>Runtime Acceptance</b>", "", "A feature shows ✅ only after a human runtime pass."]
    for feature in ACCEPTANCE_FEATURES:
        record = records.get(feature)
        if not record:
            lines.extend([f"", f"⚠️ {html.escape(feature)}", "Last Tested: Never"])
            continue
        icon = "✅" if record.get("result") == "passed" else "❌"
        lines.extend(
            [
                "",
                f"{icon} {html.escape(feature)}",
                f"Last Tested: <code>{html.escape(_display_time(record.get('created_at')))}</code>",
                f"Tester: <code>{html.escape(str(record.get('tester') or 'unknown'))}</code>",
                f"Result: <code>{html.escape(str(record.get('result') or 'unknown'))}</code>",
            ]
        )
        reason = record.get("reason")
        if reason:
            lines.append(f"Reason: {html.escape(str(reason))}")
    return "\n".join(lines)


def acceptance_dashboard_markup() -> InlineKeyboardMarkup:
    """Feature picker for manual runtime acceptance."""
    rows = [
        [InlineKeyboardButton(text=feature, callback_data=f"accept:view:{FEATURE_SLUGS[feature]}")]
        for feature in ACCEPTANCE_FEATURES
    ]
    rows.append([InlineKeyboardButton(text="Refresh", callback_data="accept:refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def acceptance_feature_markup(feature_name: str) -> InlineKeyboardMarkup:
    slug = FEATURE_SLUGS[feature_name]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Mark Passed", callback_data=f"accept:pass:{slug}"),
                InlineKeyboardButton(text="Mark Failed", callback_data=f"accept:fail:{slug}"),
            ],
            [InlineKeyboardButton(text="Back", callback_data="accept:refresh")],
        ]
    )


def render_acceptance_feature(feature_name: str) -> str:
    return (
        f"<b>{html.escape(feature_name)}</b>\n\n"
        "Only tap these after testing the full Telegram runtime flow.\n\n"
        "✅ Mark Passed = the user-visible flow worked.\n"
        "❌ Mark Failed = the runtime flow failed or timed out."
    )


async def collect_event_diagnostics(session: AsyncSession) -> dict[str, Any]:
    """Collect Academic OS pipeline diagnostics from DB and scheduler state."""
    from app.reminders.scheduler import scheduler

    counts = {
        "processed_messages": await _audit_count(session, "event_pipeline_snapshot"),
        "detected_exams": await _item_count(session, "exam"),
        "detected_assignments": await _item_count(session, "assignment"),
        "detected_quizzes": await _item_count(session, "quiz"),
        "coverage_records": await _item_count(session, "exam_coverage"),
        "pending_reminders": await _reminder_count(session, sent=False),
        "sent_reminders": await _reminder_count(session, sent=True),
        "duplicates_suppressed": await _duplicate_count(session),
        "scheduler_jobs": len(scheduler.get_jobs()),
    }
    latest = await _latest_item(session)
    return {"counts": counts, "latest_event": latest}


def render_event_diagnostics(report: dict[str, Any]) -> str:
    counts = report["counts"]
    latest = report.get("latest_event")
    lines = [
        "<b>Event Pipeline Diagnostics</b>",
        "",
        f"Processed Messages: <code>{counts['processed_messages']}</code>",
        f"Detected Exams: <code>{counts['detected_exams']}</code>",
        f"Detected Assignments: <code>{counts['detected_assignments']}</code>",
        f"Detected Quizzes: <code>{counts['detected_quizzes']}</code>",
        "",
        f"Coverage Records: <code>{counts['coverage_records']}</code>",
        "",
        f"Pending Reminders: <code>{counts['pending_reminders']}</code>",
        f"Sent Reminders: <code>{counts['sent_reminders']}</code>",
        "",
        f"Duplicates Suppressed: <code>{counts['duplicates_suppressed']}</code>",
        f"Scheduler Jobs: <code>{counts['scheduler_jobs']}</code>",
    ]
    if latest:
        lines.extend(
            [
                "",
                "<b>Latest Event</b>",
                f"{html.escape(str(latest['type']).title())}: {html.escape(str(latest['title'] or 'Untitled'))}",
                f"Thread: <code>{html.escape(str(latest['thread'] or 'unknown'))}</code>",
            ]
        )
    return "\n".join(lines)


async def collect_ask_diagnostics(session: AsyncSession) -> dict[str, Any]:
    """Collect /ask provider and media support diagnostics."""
    del session
    chat_provider = "OpenRouter/Groq" if (settings.chatbot_groq_api_key or settings.groq_api_key) else "Fallback"
    academic_provider = "Gemini Flash" if (settings.academic_gemini_api_key or settings.gemini_api_key) else "Groq/Fallback"
    return {
        "chat_provider": chat_provider,
        "academic_provider": academic_provider,
        "chat_model": settings.chatbot_model,
        "academic_model": settings.academic_extraction_model,
        "reply_pdf": True,
        "reply_docx": True,
        "reply_txt": True,
        "reply_images": True,
        "reply_voice": True,
        "reply_audio": True,
        "conversation_memory": True,
        "providers": {
            "chat": bool(settings.chatbot_groq_api_key or settings.groq_api_key),
            "academic": bool(settings.academic_gemini_api_key or settings.gemini_api_key or settings.academic_groq_api_key or settings.groq_api_key),
            "fallback": True,
        },
    }


def render_ask_diagnostics(report: dict[str, Any]) -> str:
    return "\n".join(
        [
            "<b>/ask Diagnostics</b>",
            "",
            "<b>Chat Provider</b>",
            f"{_status(report['providers']['chat'])} {html.escape(report['chat_provider'])}",
            f"Model: <code>{html.escape(report['chat_model'])}</code>",
            "",
            "<b>Academic Extraction</b>",
            f"{_status(report['providers']['academic'])} {html.escape(report['academic_provider'])}",
            f"Model: <code>{html.escape(report['academic_model'])}</code>",
            "",
            "<b>Reply Media</b>",
            f"PDF: {_status(report['reply_pdf'])}",
            f"DOCX: {_status(report['reply_docx'])}",
            f"TXT: {_status(report['reply_txt'])}",
            f"Images: {_status(report['reply_images'])}",
            f"Voice: {_status(report['reply_voice'])}",
            f"Audio: {_status(report['reply_audio'])}",
            "",
            "<b>Conversation Memory</b>",
            _status(report["conversation_memory"]),
            "",
            "<b>Fallback</b>",
            _status(report["providers"]["fallback"]),
        ]
    )


async def _item_count(session: AsyncSession, item_type: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(AcademicItem).where(AcademicItem.item_type == item_type)
    )
    return int(result.scalar() or 0)


async def _reminder_count(session: AsyncSession, *, sent: bool) -> int:
    result = await session.execute(
        select(func.count()).select_from(Reminder).where(Reminder.sent == sent)
    )
    return int(result.scalar() or 0)


async def _duplicate_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(DuplicateLog))
    return int(result.scalar() or 0)


async def _audit_count(session: AsyncSession, action: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
    )
    return int(result.scalar() or 0)


async def _latest_item(session: AsyncSession) -> dict[str, Any] | None:
    result = await session.execute(
        select(AcademicItem)
        .where(AcademicItem.item_type.in_(("exam", "assignment", "quiz")))
        .order_by(desc(AcademicItem.created_at), desc(AcademicItem.id))
        .limit(1)
    )
    item = result.scalar_one_or_none()
    if not item:
        return None
    return {
        "id": item.id,
        "type": item.item_type,
        "title": item.title,
        "thread": _thread_from_link(item.source_message_link),
    }


def _json_details(details: str | None) -> dict[str, Any]:
    if not details:
        return {}
    try:
        return json.loads(details)
    except json.JSONDecodeError:
        return {"raw": details}


def _display_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value or "unknown")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _status(ok: bool) -> str:
    return "✅" if ok else "❌"


def _thread_from_link(link: str | None) -> str | None:
    if not link:
        return None
    parts = link.rstrip("/").split("/")
    if len(parts) >= 2:
        return parts[-2]
    return None
