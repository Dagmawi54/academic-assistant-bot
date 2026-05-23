"""Events dashboard handlers for academic items, reminders, jobs, and duplicates."""

import html

from aiogram import F, Router, types
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.menus import cat_events
from app.database import crud
from app.services import event_service

router = Router(name="events")


async def _get_admin_group(session: AsyncSession, user_id: int):
    groups = await crud.get_managed_groups(session, user_id)
    return groups[0] if groups else None


def _target_label(item) -> str:
    course_name = item.course.course_name if item.course else "Unknown Course"
    topic_name = None
    if item.course and item.course.topic:
        topic_name = item.course.topic.topic_name
    return f"{course_name} / {topic_name or 'No topic'}"


@router.callback_query(F.data == "menu:cat_events")
async def cb_cat_events(callback: types.CallbackQuery) -> None:
    """Show the events management menu."""
    await callback.message.edit_text(
        "<b>Events Dashboard</b>\n\n"
        "View extracted events, reminders, scheduler jobs, low-confidence items, and duplicates.",
        reply_markup=cat_events(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "menu:events_upcoming")
async def cb_events_upcoming(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    items = await event_service.get_upcoming_events(session, group.id)
    if not items:
        await callback.message.edit_text(
            "<b>Upcoming Events</b>\n\nNo upcoming events found.",
            reply_markup=cat_events(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "<b>Upcoming Events</b>\n\n"
    for item in items:
        date_str = item.deadline.strftime("%Y-%m-%d %H:%M") if item.deadline else "No deadline"
        source = f" | <a href='{item.source_message_link}'>Source</a>" if item.source_message_link else ""
        text += f"- <b>{html.escape(_target_label(item))}</b>\n"
        text += f"  {html.escape(item.title or item.item_type)}\n"
        text += f"  <i>{date_str}</i>{source}\n"
        text += f"  <code>status={item.status} confidence={item.confidence or 0:.2f}</code>\n\n"

    await callback.message.edit_text(
        text,
        reply_markup=cat_events(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "menu:events_reminders")
async def cb_events_reminders(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    reminders = await event_service.get_scheduled_reminders(session, group.id)
    if not reminders:
        await callback.message.edit_text(
            "<b>Scheduled Reminders</b>\n\nNo scheduled reminders pending.",
            reply_markup=cat_events(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "<b>Scheduled Reminders</b>\n\n"
    for reminder in reminders:
        item = reminder.academic_item
        text += f"- <b>{reminder.send_time.strftime('%Y-%m-%d %H:%M')}</b>\n"
        text += f"  {html.escape(_target_label(item))}: {html.escape(item.title or item.item_type)}\n"
        text += f"  <code>reminder_id={reminder.id} thread_id={reminder.thread_id or 'main'}</code>\n\n"

    await callback.message.edit_text(text, reply_markup=cat_events(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "menu:events_scheduler")
async def cb_events_scheduler(callback: types.CallbackQuery) -> None:
    jobs = event_service.get_scheduler_jobs()
    if not jobs:
        await callback.message.edit_text(
            "<b>Scheduler Jobs</b>\n\nNo APScheduler jobs are currently registered.",
            reply_markup=cat_events(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "<b>Scheduler Jobs</b>\n\n"
    for job in jobs[:30]:
        text += f"- <code>{html.escape(str(job['job_id']))}</code>\n"
        text += f"  reminder_id={html.escape(str(job['reminder_id']))}\n"
        text += f"  next={html.escape(str(job['next_run_time']))}\n\n"

    await callback.message.edit_text(text, reply_markup=cat_events(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.in_({"menu:events_coverage", "menu:exam_coverage"}))
async def cb_events_coverage(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    items = await event_service.get_exam_coverages(session, group.id)
    if not items:
        await callback.message.edit_text(
            "<b>Exam Coverages</b>\n\nNo exam coverages found.",
            reply_markup=cat_events(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "<b>Exam Coverages</b>\n\n"
    for item in items:
        source = f"<a href='{item.source_message_link}'>Source</a>" if item.source_message_link else ""
        text += f"- <b>{html.escape(_target_label(item))}</b>\n"
        text += f"  Coverage: {html.escape(item.coverage or 'Unknown')}\n"
        if source:
            text += f"  {source}\n"
        text += "\n"

    await callback.message.edit_text(
        text,
        reply_markup=cat_events(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "menu:events_review")
async def cb_events_review(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    items = await event_service.get_low_confidence_items(session, group.id)
    if not items:
        await callback.message.edit_text(
            "<b>Low Confidence Extractions</b>\n\nNo items currently pending review.",
            reply_markup=cat_events(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "<b>Low Confidence Extractions</b>\n\n"
    for item in items:
        score = item.confidence or 0.0
        text += f"- <b>[ID {item.id}]</b> {html.escape(item.item_type)} (Score: {score:.2f})\n"
        text += f"  {html.escape(_target_label(item))}\n"
        if item.source_message_link:
            text += f"  <a href='{item.source_message_link}'>View Message</a>\n"
        text += "\n"

    await callback.message.edit_text(
        text,
        reply_markup=cat_events(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "menu:events_duplicates")
async def cb_events_duplicates(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    logs = await event_service.get_suppressed_duplicates(session, group.id)
    if not logs:
        await callback.message.edit_text(
            "<b>Suppressed Duplicates</b>\n\nNo duplicates have been suppressed.",
            reply_markup=cat_events(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "<b>Suppressed Duplicates</b>\n\n"
    for log in logs:
        text += f"- <b>Original ID {log.existing_item_id}</b>: {html.escape(log.reason)}\n"
        text += f"  <i>{log.created_at.strftime('%Y-%m-%d %H:%M')}</i>\n\n"

    await callback.message.edit_text(text, reply_markup=cat_events(), parse_mode="HTML")
    await callback.answer()
