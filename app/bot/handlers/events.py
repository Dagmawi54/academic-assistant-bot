"""Events and Dashboard handlers for searching through academic items and reminders."""

from aiogram import Router, types, F
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession
import html

from app.admin.permissions import require_role
from app.admin.menus import cat_events
from app.services import event_service
from app.database import crud
from app.logging import get_logger

logger = get_logger("events_handler")
router = Router(name="events")


async def _get_admin_group(session: AsyncSession, user_id: int):
    groups = await crud.get_managed_groups(session, user_id)
    return groups[0] if groups else None


@router.callback_query(F.data == "menu:cat_events", require_role(["owner", "dept_admin", "section_admin", "representative", "moderator"]))
async def cb_cat_events(callback: types.CallbackQuery) -> None:
    """Show the events management menu."""
    await callback.message.edit_text(
        "📋 <b>Events &amp; Dashboard</b>\n\n"
        "View extracted events, upcoming deadlines, exam coverages, and system duplicates.",
        reply_markup=cat_events(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "menu:events_upcoming", require_role(["owner", "dept_admin", "section_admin", "representative", "moderator"]))
async def cb_events_upcoming(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    items = await event_service.get_upcoming_events(session, group.id)
    if not items:
        await callback.message.edit_text(
            "📅 <b>Upcoming Events</b>\n\nNo upcoming events found.",
            reply_markup=cat_events(),
            parse_mode="HTML"
        )
        return

    text = "📅 <b>Upcoming Events</b>\n\n"
    for item in items:
        date_str = item.deadline.strftime("%Y-%m-%d %H:%M") if item.deadline else "No deadline"
        course_name = item.course.course_name if item.course else "Unknown Course"
        link_str = f"| <a href='{item.source_message_link}'>Source</a>" if item.source_message_link else ""
        text += f"• <b>{html.escape(course_name)}</b>: {html.escape(item.title or item.item_type)}\n"
        text += f"  <i>{date_str}</i> {link_str}\n\n"

    await callback.message.edit_text(text, reply_markup=cat_events(), parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data == "menu:events_reminders", require_role(["owner", "dept_admin", "section_admin", "representative", "moderator"]))
async def cb_events_reminders(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    reminders = await event_service.get_scheduled_reminders(session, group.id)
    if not reminders:
        await callback.message.edit_text(
            "⏰ <b>Scheduled Reminders</b>\n\nNo scheduled reminders pending.",
            reply_markup=cat_events(),
            parse_mode="HTML"
        )
        return

    text = "⏰ <b>Scheduled Reminders</b>\n\n"
    for r in reminders:
        item = r.academic_item
        course_name = item.course.course_name if item.course else "Unknown Course"
        text += f"• <b>{r.send_time.strftime('%Y-%m-%d %H:%M')}</b>\n"
        text += f"  <i>{html.escape(course_name)}: {html.escape(item.title or item.item_type)}</i>\n\n"

    await callback.message.edit_text(text, reply_markup=cat_events(), parse_mode="HTML")


@router.callback_query(F.data == "menu:events_coverage", require_role(["owner", "dept_admin", "section_admin", "representative", "moderator"]))
async def cb_events_coverage(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    items = await event_service.get_exam_coverages(session, group.id)
    if not items:
        await callback.message.edit_text(
            "📝 <b>Exam Coverages</b>\n\nNo exam coverages found.",
            reply_markup=cat_events(),
            parse_mode="HTML"
        )
        return

    text = "📝 <b>Exam Coverages</b>\n\n"
    for item in items:
        course_name = item.course.course_name if item.course else "Unknown Course"
        link_str = f"<a href='{item.source_message_link}'>Source</a>" if item.source_message_link else ""
        text += f"• <b>{html.escape(course_name)}</b>\n"
        text += f"  Coverage: {html.escape(item.coverage or 'Unknown')}\n"
        if link_str:
            text += f"  {link_str}\n"
        text += "\n"

    await callback.message.edit_text(text, reply_markup=cat_events(), parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data == "menu:events_review", require_role(["owner", "dept_admin", "section_admin", "representative", "moderator"]))
async def cb_events_review(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    items = await event_service.get_low_confidence_items(session, group.id)
    if not items:
        await callback.message.edit_text(
            "⚠️ <b>Low Confidence Extractions</b>\n\nNo items currently pending review.",
            reply_markup=cat_events(),
            parse_mode="HTML"
        )
        return

    text = "⚠️ <b>Low Confidence Extractions</b>\n\n"
    for item in items:
        score = item.confidence or 0.0
        text += f"• <b>[ID {item.id}]</b> {html.escape(item.item_type)} (Score: {score:.2f})\n"
        if item.source_message_link:
            text += f"  <a href='{item.source_message_link}'>View Message</a>\n"
        text += "\n"

    await callback.message.edit_text(text, reply_markup=cat_events(), parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data == "menu:events_duplicates", require_role(["owner", "dept_admin", "section_admin", "representative", "moderator"]))
async def cb_events_duplicates(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    logs = await event_service.get_suppressed_duplicates(session, group.id)
    if not logs:
        await callback.message.edit_text(
            "🗑️ <b>Suppressed Duplicates</b>\n\nNo duplicates have been suppressed.",
            reply_markup=cat_events(),
            parse_mode="HTML"
        )
        return

    text = "🗑️ <b>Suppressed Duplicates</b>\n\n"
    for log in logs:
        text += f"• <b>Original ID {log.existing_item_id}</b>: {html.escape(log.reason)}\n"
        text += f"  <i>{log.created_at.strftime('%Y-%m-%d %H:%M')}</i>\n\n"

    await callback.message.edit_text(text, reply_markup=cat_events(), parse_mode="HTML")
