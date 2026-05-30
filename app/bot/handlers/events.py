"""Events dashboard handlers for academic items, reminders, jobs, and duplicates."""

import html

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.menus import cat_events
from app.admin.states import EventEditStates
from app.database import crud
from app.database.models import AcademicItem
from app.events.bus import ASSIGNMENT_DETECTED, EXAM_DETECTED, emit
from app.services import event_service
from app.services.coverage_parser import render_coverage_summary
from app.services.exam_coverage_service import stitch_item_coverage

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


def _events_back_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="menu:cat_events")]]
    )


def _section_nav(refresh_data: str, back_data: str = "menu:cat_events") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Refresh", callback_data=refresh_data)],
            [InlineKeyboardButton(text="Back", callback_data=back_data)],
        ]
    )


def _paged_nav(prefix: str, page: int, has_next: bool, back_data: str = "menu:cat_events") -> InlineKeyboardMarkup:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="Prev", callback_data=f"{prefix}:{page - 1}"))
    if has_next:
        row.append(InlineKeyboardButton(text="Next", callback_data=f"{prefix}:{page + 1}"))
    buttons = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="Refresh", callback_data=f"{prefix}:{page}")])
    buttons.append([InlineKeyboardButton(text="Back", callback_data=back_data)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _duplicate_detail_back_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="menu:events_duplicates")]]
    )


def _duplicates_markup(logs) -> InlineKeyboardMarkup:
    buttons = []
    for log in logs[:10]:
        label = f"{log.created_at.strftime('%m-%d %H:%M')} • item {log.existing_item_id}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"events:duplicate:{log.id}")])
    buttons.append([InlineKeyboardButton(text="Back", callback_data="menu:cat_events")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _coverage_list_markup(items) -> InlineKeyboardMarkup:
    buttons = []
    for item in items[:10]:
        course = item.course.course_name if item.course else "Unknown"
        buttons.append([InlineKeyboardButton(text=f"{course} #{item.id}", callback_data=f"coverage:detail:{item.id}")])
    buttons.append([InlineKeyboardButton(text="Refresh", callback_data="menu:events_coverage")])
    buttons.append([InlineKeyboardButton(text="Back", callback_data="menu:cat_events")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _coverage_detail_markup(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Exact Coverage", callback_data=f"coverage:exact:{item_id}")],
            [InlineKeyboardButton(text="Confirm & Stitch", callback_data=f"coverage:stitch:{item_id}")],
            [InlineKeyboardButton(text="Edit Coverage", callback_data=f"coverage:edit:{item_id}")],
            [InlineKeyboardButton(text="Coverage History", callback_data=f"coverage:history:{item_id}")],
            [InlineKeyboardButton(text="Back", callback_data="menu:events_coverage")],
        ]
    )


def _review_list_markup(items) -> InlineKeyboardMarkup:
    buttons = []
    for item in items[:10]:
        label = f"#{item.id} {item.item_type} {item.confidence or 0:.2f}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"events:review:item:{item.id}")])
    buttons.append([InlineKeyboardButton(text="Refresh", callback_data="menu:events_review")])
    buttons.append([InlineKeyboardButton(text="Back", callback_data="menu:cat_events")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _review_detail_markup(item: AcademicItem) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Approve", callback_data=f"events:review:approve:{item.id}")],
        [InlineKeyboardButton(text="Reject", callback_data=f"events:review:reject:{item.id}")],
        [InlineKeyboardButton(text="Edit", callback_data=f"events:review:edit:{item.id}")],
    ]
    if item.source_message_link:
        buttons.append([InlineKeyboardButton(text="View Source Message", url=item.source_message_link)])
    buttons.append([InlineKeyboardButton(text="Back", callback_data="menu:events_review")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _items_markup(items, *, back_data: str, refresh_data: str) -> InlineKeyboardMarkup:
    buttons = []
    for item in items[:10]:
        label = f"#{item.id} {item.item_type.title()} - {item.course.course_name if item.course else 'No course'}"
        buttons.append([InlineKeyboardButton(text=label[:64], callback_data=f"events:item:{item.id}")])
    buttons.append([InlineKeyboardButton(text="Refresh", callback_data=refresh_data)])
    buttons.append([InlineKeyboardButton(text="Back", callback_data=back_data)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _item_detail_markup(item: AcademicItem, back_data: str = "menu:events_upcoming") -> InlineKeyboardMarkup:
    buttons = []
    if item.source_message_link:
        buttons.append([InlineKeyboardButton(text="Source Message", url=item.source_message_link)])
    buttons.append([InlineKeyboardButton(text="Back", callback_data=back_data)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


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
    await _render_upcoming_events(callback, session, page=0, item_types=None, title="Upcoming Events", refresh_prefix="events:upcoming")


@router.callback_query(F.data == "menu:events_exams")
async def cb_events_exams(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _render_upcoming_events(callback, session, page=0, item_types=("exam",), title="Upcoming Exams", refresh_prefix="events:exams")


@router.callback_query(F.data == "menu:events_assignments")
async def cb_events_assignments(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _render_upcoming_events(callback, session, page=0, item_types=("assignment",), title="Upcoming Assignments", refresh_prefix="events:assignments")


@router.callback_query(F.data == "menu:events_quizzes")
async def cb_events_quizzes(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _render_upcoming_events(callback, session, page=0, item_types=("quiz",), title="Upcoming Quizzes", refresh_prefix="events:quizzes")


@router.callback_query(F.data.startswith("events:upcoming:"))
async def cb_events_upcoming_page(callback: types.CallbackQuery, session: AsyncSession) -> None:
    page = int(callback.data.rsplit(":", 1)[1])
    await _render_upcoming_events(callback, session, page=page, item_types=None, title="Upcoming Events", refresh_prefix="events:upcoming")


@router.callback_query(F.data.startswith("events:exams:"))
async def cb_events_exams_page(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _render_upcoming_events(callback, session, page=int(callback.data.rsplit(":", 1)[1]), item_types=("exam",), title="Upcoming Exams", refresh_prefix="events:exams")


@router.callback_query(F.data.startswith("events:assignments:"))
async def cb_events_assignments_page(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _render_upcoming_events(callback, session, page=int(callback.data.rsplit(":", 1)[1]), item_types=("assignment",), title="Upcoming Assignments", refresh_prefix="events:assignments")


@router.callback_query(F.data.startswith("events:quizzes:"))
async def cb_events_quizzes_page(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _render_upcoming_events(callback, session, page=int(callback.data.rsplit(":", 1)[1]), item_types=("quiz",), title="Upcoming Quizzes", refresh_prefix="events:quizzes")


async def _render_upcoming_events(
    callback: types.CallbackQuery,
    session: AsyncSession,
    page: int,
    *,
    item_types,
    title: str,
    refresh_prefix: str,
) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    per_page = 8
    items = await event_service.get_upcoming_events(session, group.id, item_types=item_types, limit=per_page + 1, offset=page * per_page)
    has_next = len(items) > per_page
    items = items[:per_page]
    if not items:
        await callback.message.edit_text(
            f"<b>{html.escape(title)}</b>\n\nNo matching events found.",
            reply_markup=_section_nav(f"{refresh_prefix}:0"),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    reminder_counts = await event_service.get_reminder_counts(session, [item.id for item in items])
    text = f"<b>{html.escape(title)}</b>\nPage {page + 1}\n\n"
    for item in items:
        date_str = item.deadline.strftime("%Y-%m-%d %H:%M") if item.deadline else "No deadline"
        source = f" | <a href='{item.source_message_link}'>Source</a>" if item.source_message_link else ""
        course_name = item.course.course_name if item.course else "Unknown Course"
        topic_name = item.course.topic.topic_name if item.course and item.course.topic else "No topic"
        text += f"- <b>#{item.id} {html.escape(item.title or item.item_type.title())}</b>\n"
        text += f"  Course: {html.escape(course_name)}\n"
        text += f"  Topic: {html.escape(topic_name)}\n"
        text += f"  Date: <i>{date_str}</i>\n"
        text += f"  Reminders: <code>{reminder_counts.get(item.id, 0)}</code>{source}\n\n"

    await callback.message.edit_text(
        text,
        reply_markup=_items_markup(items, back_data="menu:cat_events", refresh_data=f"{refresh_prefix}:{page}"),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "menu:events_recent")
async def cb_events_recent(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _render_recent_events(callback, session, page=0)


@router.callback_query(F.data.startswith("events:recent:"))
async def cb_events_recent_page(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _render_recent_events(callback, session, page=int(callback.data.rsplit(":", 1)[1]))


async def _render_recent_events(callback: types.CallbackQuery, session: AsyncSession, page: int) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return
    per_page = 10
    items = await event_service.get_recent_items(session, group.id, limit=per_page, offset=page * per_page)
    if not items:
        await callback.message.edit_text(
            "<b>Recently Detected</b>\n\nNo academic items have been detected yet.",
            reply_markup=_section_nav("menu:events_recent"),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    text = "<b>Recently Detected</b>\n\n"
    for item in items:
        course = item.course.course_name if item.course else "No course"
        detected = item.created_at.strftime("%Y-%m-%d %H:%M") if item.created_at else "Unknown"
        text += f"- <b>#{item.id} {html.escape(item.item_type.title())}</b> · {html.escape(course)}\n"
        text += f"  {html.escape(item.title or 'Untitled')} · <code>{detected}</code>\n"
    await callback.message.edit_text(
        text,
        reply_markup=_items_markup(items, back_data="menu:cat_events", refresh_data=f"events:recent:{page}"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("events:item:"))
async def cb_events_item_detail(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await event_service.get_item_detail(session, group.id, item_id)
    if not item:
        await callback.answer("Event not found.", show_alert=True)
        return
    course = item.course.course_name if item.course else "Unknown Course"
    topic = item.course.topic.topic_name if item.course and item.course.topic else "No topic"
    deadline = item.deadline.strftime("%Y-%m-%d %H:%M") if item.deadline else "No deadline"
    reminders = sorted(item.reminders, key=lambda r: r.send_time)
    reminder_lines = "\n".join(
        f"• <code>{r.send_time.strftime('%Y-%m-%d %H:%M')}</code> {'sent' if r.sent else 'pending'}"
        for r in reminders
    ) or "No reminders scheduled."
    text = (
        f"<b>Event #{item.id}</b>\n\n"
        f"<blockquote>"
        f"Course: {html.escape(course)}\n"
        f"Topic: {html.escape(topic)}\n"
        f"Type: {html.escape(item.item_type.title())}\n"
        f"Deadline: {html.escape(deadline)}\n"
        f"Status: {html.escape(item.status)}"
        f"</blockquote>\n\n"
        f"<b>Reminder Schedule</b>\n{reminder_lines}\n\n"
        f"<b>Detected From</b>\n{html.escape((item.raw_text or 'Unavailable')[:700])}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=_item_detail_markup(item),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer("Event opened.")


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
            reply_markup=_section_nav("menu:events_reminders"),
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

    await callback.message.edit_text(text, reply_markup=_section_nav("menu:events_reminders"), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "menu:events_scheduler")
async def cb_events_scheduler(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    jobs = await event_service.get_scheduler_job_details(session, group.id)
    if not jobs:
        await callback.message.edit_text(
            "<b>Scheduler Jobs</b>\n\nNo APScheduler jobs are currently registered.",
            reply_markup=_section_nav("menu:events_scheduler"),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "<b>Scheduler Jobs</b>\n\n"
    for job in jobs[:30]:
        text += f"- <code>{html.escape(str(job['job_id']))}</code>\n"
        text += f"  course={html.escape(str(job.get('course') or 'Unknown'))}\n"
        text += f"  topic={html.escape(str(job.get('topic') or 'Unknown'))}\n"
        text += f"  type={html.escape(str(job.get('reminder_type') or 'reminder'))}\n"
        text += f"  reminder_id={html.escape(str(job['reminder_id']))}\n"
        text += f"  next={html.escape(str(job['next_run_time']))}\n\n"

    await callback.message.edit_text(text, reply_markup=_section_nav("menu:events_scheduler"), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "menu:events_coverage")
async def cb_events_coverage(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    items = await event_service.get_exam_coverages(session, group.id)
    if not items:
        await callback.message.edit_text(
            "<b>Exam Coverages</b>\n\nNo exam coverages found.",
            reply_markup=_section_nav("menu:events_coverage"),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "<b>Exam Coverages</b>\n\n"
    for item in items:
        source = f"<a href='{item.source_message_link}'>Source</a>" if item.source_message_link else ""
        text += f"- <b>{html.escape(_target_label(item))}</b>\n"
        text += f"  Coverage: {html.escape(render_coverage_summary(item.coverage))}\n"
        if source:
            text += f"  {source}\n"
        text += "\n"

    await callback.message.edit_text(
        text,
        reply_markup=_coverage_list_markup(items),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("coverage:detail:"))
async def cb_coverage_detail(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await event_service.get_coverage_item(session, group.id, item_id)
    if not item:
        await callback.answer("Coverage item not found.", show_alert=True)
        return
    text = (
        "<b>Exam Coverage</b>\n\n"
        f"Course/Topic: <b>{html.escape(_target_label(item))}</b>\n"
        f"Summary: {html.escape(render_coverage_summary(item.coverage))}\n"
        f"Status: <code>{html.escape(item.status)}</code>\n"
        f"Item: <code>{item.id}</code>"
    )
    await callback.message.edit_text(text, reply_markup=_coverage_detail_markup(item.id), parse_mode="HTML")
    await callback.answer("Coverage opened.")


@router.callback_query(F.data.startswith("coverage:exact:"))
async def cb_coverage_exact(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await event_service.get_coverage_item(session, group.id, item_id)
    if not item:
        await callback.answer("Coverage item not found.", show_alert=True)
        return
    text = (
        "<b>Exact Coverage</b>\n\n"
        f"{html.escape(item.coverage or item.raw_text or 'No coverage recorded')}"
    )
    await callback.message.edit_text(text, reply_markup=_coverage_detail_markup(item.id), parse_mode="HTML")
    await callback.answer("Exact coverage shown.")


@router.callback_query(F.data.startswith("coverage:history:"))
async def cb_coverage_history(callback: types.CallbackQuery, session: AsyncSession) -> None:
    from sqlalchemy import desc, select
    from app.database.models import AuditLog

    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await event_service.get_coverage_item(session, group.id, item_id)
    if not item:
        await callback.answer("Coverage item not found.", show_alert=True)
        return
    rows = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.action.in_(("exam_coverage_created", "exam_coverage_stitched", "exam_coverage_edited")))
            .order_by(desc(AuditLog.created_at))
            .limit(10)
        )
    ).scalars().all()
    matching = [row for row in rows if row.details and f"item_id={item_id}" in row.details]
    text = f"<b>Coverage History</b>\n\nItem <code>{item_id}</code>\n\n"
    if matching:
        for row in matching:
            text += f"• <code>{row.created_at.strftime('%Y-%m-%d %H:%M')}</code> {html.escape(row.action)}\n"
    else:
        text += "No history entries found yet."
    await callback.message.edit_text(text, reply_markup=_coverage_detail_markup(item.id), parse_mode="HTML")
    await callback.answer("Coverage history shown.")


@router.callback_query(F.data.startswith("coverage:edit:"))
async def cb_coverage_edit(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await event_service.get_coverage_item(session, group.id, item_id)
    if not item:
        await callback.answer("Coverage item not found.", show_alert=True)
        return
    await state.update_data(coverage_item_id=item.id, group_id=group.id)
    await state.set_state(EventEditStates.waiting_coverage_edit)
    await callback.message.edit_text(
        "<b>Edit Coverage</b>\n\nSend the corrected coverage text.\n\nExample: <code>Chapters 1-8 except recursion</code>",
        reply_markup=_coverage_detail_markup(item.id),
        parse_mode="HTML",
    )
    await callback.answer("Send the new coverage text.")


@router.message(EventEditStates.waiting_coverage_edit, F.chat.type == "private")
async def msg_coverage_edit(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    from app.services.coverage_parser import dump_coverage, parse_coverage_text

    data = await state.get_data()
    item = await event_service.get_coverage_item(session, data["group_id"], data["coverage_item_id"])
    if not item:
        await message.answer("Coverage item not found.", parse_mode=None)
        await state.clear()
        return
    item.raw_text = message.text.strip()
    item.coverage = dump_coverage(parse_coverage_text(item.raw_text))
    item.status = "active"
    item.version = (item.version or 1) + 1
    await session.flush()
    await crud.log_action(
        session,
        action="exam_coverage_edited",
        telegram_user_id=message.from_user.id,
        chat_id=item.source_chat_id,
        details=f"item_id={item.id}",
    )
    await state.clear()
    await message.answer(
        f"📌 <b>Coverage updated.</b>\n\n{html.escape(render_coverage_summary(item.coverage))}",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("coverage:stitch:"))
async def cb_coverage_stitch(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    scoped_item = await event_service.get_coverage_item(session, group.id, item_id)
    if not scoped_item:
        await callback.answer("Coverage item not found.", show_alert=True)
        return
    item = await stitch_item_coverage(session, item_id)
    if not item:
        await callback.answer("Coverage stitch failed.", show_alert=True)
        return
    text = (
        "✅ <b>Coverage successfully updated</b>\n\n"
        f"{html.escape(render_coverage_summary(item.coverage))}\n\n"
        f"<code>item_id={item.id}</code>"
    )
    await callback.message.edit_text(text, reply_markup=_coverage_detail_markup(item.id), parse_mode="HTML")
    await callback.answer("Coverage successfully updated.")


@router.callback_query(F.data == "coverage:stitch")
async def cb_coverage_stitch_missing_item(callback: types.CallbackQuery) -> None:
    await callback.answer("Open a coverage item first, then use Confirm & Stitch.", show_alert=True)


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
            reply_markup=_section_nav("menu:events_review"),
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
        reply_markup=_review_list_markup(items),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("events:review:item:"))
async def cb_events_review_item(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await event_service.get_item_for_review(session, group.id, item_id)
    if not item:
        await callback.answer("Review item not found.", show_alert=True)
        return
    deadline = item.deadline.strftime("%Y-%m-%d %H:%M") if item.deadline else "None"
    text = (
        "<b>Pending AI Review</b>\n\n"
        f"Type: <code>{html.escape(item.item_type)}</code>\n"
        f"Title: {html.escape(item.title or 'Untitled')}\n"
        f"Course/Topic: {html.escape(_target_label(item))}\n"
        f"Deadline: <code>{deadline}</code>\n"
        f"Confidence: <code>{item.confidence or 0:.2f}</code>\n\n"
        f"<b>Source Text</b>\n{html.escape((item.raw_text or 'Unavailable')[:900])}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=_review_detail_markup(item),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer("Review item opened.")


@router.callback_query(F.data.startswith("events:review:approve:"))
async def cb_events_review_approve(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _review_action(callback, session, approved=True)


@router.callback_query(F.data.startswith("events:review:reject:"))
async def cb_events_review_reject(callback: types.CallbackQuery, session: AsyncSession) -> None:
    await _review_action(callback, session, approved=False)


@router.callback_query(F.data.startswith("events:review:edit:"))
async def cb_events_review_edit(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await event_service.get_item_for_review(session, group.id, item_id)
    if not item:
        await callback.answer("Review item not found.", show_alert=True)
        return
    await state.update_data(review_item_id=item.id, group_id=group.id)
    await state.set_state(EventEditStates.waiting_item_edit)
    await callback.message.edit_text(
        "<b>Edit Review Item</b>\n\n"
        "Send corrections using any of these lines:\n"
        "<code>type: exam</code>\n"
        "<code>title: Midterm Exam</code>\n"
        "<code>deadline: 2026-06-02 09:00</code>",
        reply_markup=_review_detail_markup(item),
        parse_mode="HTML",
    )
    await callback.answer("Send the corrections.")


@router.message(EventEditStates.waiting_item_edit, F.chat.type == "private")
async def msg_review_item_edit(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    from dateutil import parser as dateparser

    data = await state.get_data()
    item = await event_service.get_item_for_review(session, data["group_id"], data["review_item_id"])
    if not item:
        await message.answer("Review item not found.", parse_mode=None)
        await state.clear()
        return
    updates = _parse_admin_item_edit(message.text or "")
    if "item_type" in updates:
        item.item_type = updates["item_type"]
    if "title" in updates:
        item.title = updates["title"]
    if "deadline" in updates:
        item.deadline = dateparser.parse(updates["deadline"], fuzzy=True)
    item.status = "active"
    item.version = (item.version or 1) + 1
    await session.flush()
    if "deadline" in updates:
        from app.services.reminder_service import create_reminders_for_item_in_session

        await crud.cancel_item_reminders(session, item.id)
        await create_reminders_for_item_in_session(session, item)
    await crud.log_action(
        session,
        action="review_item_edited",
        telegram_user_id=message.from_user.id,
        chat_id=item.source_chat_id,
        details=f"item_id={item.id}",
    )
    await state.clear()
    await message.answer(f"📌 <b>Event updated.</b>\n\nItem <code>{item.id}</code> is now active.", parse_mode="HTML")


async def _review_action(callback: types.CallbackQuery, session: AsyncSession, *, approved: bool) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await event_service.get_item_for_review(session, group.id, item_id)
    if not item:
        await callback.answer("Review item not found.", show_alert=True)
        return
    if approved:
        await crud.update_fields(session, AcademicItem, item.id, status="active")
        event = ASSIGNMENT_DETECTED if item.item_type == "assignment" else EXAM_DETECTED
        if item.item_type in {"assignment", "exam", "quiz"}:
            await emit(event, item_id=item.id)
        text = f"✅ <b>Approved</b>\n\n{html.escape(item.title or item.item_type)}"
        await callback.answer("Item approved.")
    else:
        await crud.update_fields(session, AcademicItem, item.id, status="rejected")
        text = f"Rejected.\n\n{html.escape(item.title or item.item_type)}"
        await callback.answer("Item rejected.")
    await callback.message.edit_text(text, reply_markup=_section_nav("menu:events_review"), parse_mode="HTML")


def _parse_admin_item_edit(text: str) -> dict[str, str]:
    updates: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key == "type":
            updates["item_type"] = value.lower().replace(" ", "_")
        elif key in {"title", "deadline"}:
            updates[key] = value
    return updates


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
            reply_markup=_events_back_markup(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    text = "<b>Suppressed Duplicates</b>\n\nTap a record to inspect exactly why it was suppressed."
    for log in logs:
        text += f"\n• <b>Item {log.existing_item_id}</b> — <i>{log.created_at.strftime('%Y-%m-%d %H:%M')}</i>"

    await callback.message.edit_text(
        text,
        reply_markup=_duplicates_markup(logs),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("events:duplicate:"))
async def cb_duplicate_detail(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group = await _get_admin_group(session, callback.from_user.id)
    if not group:
        await callback.answer("No active group.", show_alert=True)
        return

    duplicate_id = int(callback.data.rsplit(":", 1)[1])
    log = await event_service.get_duplicate_detail(session, group.id, duplicate_id)
    if not log:
        await callback.answer("Duplicate record not found.", show_alert=True)
        return

    from app.database.models import AcademicItem
    existing_item = await crud.get_by_id(session, AcademicItem, log.existing_item_id)

    similarity = "0.92" if "semantic" in (log.reason or "").lower() else "0.75"
    lines = [
        "<b>Suppressed Duplicate</b>",
        "",
        f"<b>Original message</b>\n{html.escape(log.raw_text or 'Unavailable')}",
        "",
        f"<b>Matched event</b>\n{html.escape(existing_item.title if existing_item and existing_item.title else 'Unknown event')}",
        f"<b>Suppression reason</b>\n{html.escape(log.reason or 'No reason recorded')}",
        f"<b>Similarity score</b>\n<code>{similarity}</code>",
        f"<b>Timestamp</b>\n<code>{log.created_at.strftime('%Y-%m-%d %H:%M')}</code>",
    ]

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=_duplicate_detail_back_markup(),
        parse_mode="HTML",
    )
    await callback.answer()
