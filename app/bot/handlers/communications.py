"""Communications DM handler: announcements, direct broadcasts, targeted pushes, coverage."""

from __future__ import annotations

import html

from aiogram import Bot, F, Router, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import menus
from app.admin.states import AnnouncementStates, ExamCoverageStates
from app.database import crud
from app.database.models import Course, Group, Topic
from app.logging import get_logger
from app.services.announcement_formatter import format_announcement_html
from app.services.exam_coverage_service import create_exam_coverage_entry

logger = get_logger("communications_handler")
router = Router(name="communications")


@router.callback_query(F.data == "menu:announcements")
async def start_communication(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Start announcement flow."""
    target_type = "announcement"

    await state.clear()
    await state.update_data(target_type=target_type)

    managed_groups = await crud.get_managed_groups(session, callback.from_user.id)
    if not managed_groups:
        await callback.message.edit_text(
            "You do not manage any registered groups.",
            reply_markup=menus.back_button(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if len(managed_groups) == 1:
        await _process_group_selection(callback, state, session, managed_groups[0].id)
        return

    await state.set_state(AnnouncementStates.waiting_group_select)
    await callback.message.edit_text(
        "<b>Select the group:</b>",
        reply_markup=menus.group_select(managed_groups),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(AnnouncementStates.waiting_group_select, F.data.startswith("group:"))
async def cb_select_group(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await _process_group_selection(callback, state, session, int(callback.data.split(":")[1]))


async def _process_group_selection(
    callback: types.CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    group_id: int,
) -> None:
    await state.update_data(group_id=group_id)
    data = await state.get_data()
    target_type = data["target_type"]
    group = await crud.get_by_id(session, Group, group_id)

    if target_type == "announcement":
        courses = await crud.get_active_courses(session, group_id)
        await state.update_data(selected_course_ids=[], target_scope=None, target_topic_ids=[], target_names=[])
        await state.set_state(AnnouncementStates.waiting_destination)
        await callback.message.edit_text(
            "<b>Announcement Targets</b>\n\n"
            "Select one or more course topics, choose General, or choose Global.",
            reply_markup=menus.announcement_target_select(courses),
            parse_mode="HTML",
        )
        await callback.answer()
        return


@router.callback_query(AnnouncementStates.waiting_destination, F.data.startswith("ann:toggle_course:"))
async def cb_toggle_announcement_course(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    course_id = int(callback.data.rsplit(":", 1)[1])
    data = await state.get_data()
    selected = set(data.get("selected_course_ids") or [])
    if course_id in selected:
        selected.remove(course_id)
    else:
        selected.add(course_id)

    courses = await crud.get_active_courses(session, data["group_id"])
    selected_names = [course.course_name for course in courses if course.id in selected]
    await state.update_data(selected_course_ids=list(selected), target_scope=None, target_topic_ids=[], target_names=selected_names)
    await callback.message.edit_text(
        "<b>Announcement Targets</b>\n\nSelected course topics will receive one message each.",
        reply_markup=menus.announcement_target_select(courses, selected_course_ids=selected),
        parse_mode="HTML",
    )
    await callback.answer("Target updated.")


@router.callback_query(AnnouncementStates.waiting_destination, F.data.in_({"ann:target_general", "ann:target_global"}))
async def cb_set_announcement_scope(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    scope = "general" if callback.data == "ann:target_general" else "global"
    await state.update_data(
        target_scope=scope,
        selected_course_ids=[],
        target_topic_ids=[],
        target_names=["General"] if scope == "general" else ["Global: all configured topics"],
    )
    targets = await _resolve_announcement_targets(session, await state.get_data())
    if not targets:
        await callback.answer("No valid target topics found.", show_alert=True)
        return

    await state.update_data(
        target_topic_ids=[topic.id for topic in targets],
        target_names=[topic.topic_name for topic in targets],
    )
    await state.set_state(AnnouncementStates.waiting_content)
    target_lines = "\n".join(f"â€¢ {html.escape(topic.topic_name)}" for topic in targets)
    await callback.message.edit_text(
        f"<b>Targets</b>\n{target_lines}\n\nSend the announcement text or media. Text will be lightly cleaned before posting.",
        reply_markup=menus.cancel_button(),
        parse_mode="HTML",
    )
    await callback.answer(f"{scope.title()} target ready.")


@router.callback_query(AnnouncementStates.waiting_destination, F.data == "ann:targets_done")
async def cb_announcement_targets_done(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    targets = await _resolve_announcement_targets(session, data)
    if not targets:
        await callback.answer("Select at least one valid target.", show_alert=True)
        return

    await state.update_data(
        target_topic_ids=[topic.id for topic in targets],
        target_names=[topic.topic_name for topic in targets],
    )
    await state.set_state(AnnouncementStates.waiting_content)
    target_lines = "\n".join(f"• {html.escape(topic.topic_name)}" for topic in targets)
    prompt = "Send the announcement text or media. Text will be lightly cleaned before posting."
    await callback.message.edit_text(
        f"<b>Targets</b>\n{target_lines}\n\n{prompt}",
        reply_markup=menus.cancel_button(),
        parse_mode="HTML",
    )
    await callback.answer("Targets ready.")


@router.callback_query(AnnouncementStates.waiting_destination, F.data.startswith("course:"))
async def cb_select_course(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Legacy targeted-push course selector; resolves the selected course to its topic."""
    course_id = int(callback.data.split(":", 1)[1])
    course = await crud.get_by_id(session, Course, course_id)
    if not course:
        await callback.answer("Course not found.", show_alert=True)
        return
    if not course.topic_id:
        await callback.answer("This course is not linked to a topic.", show_alert=True)
        return

    topic = await crud.get_by_id(session, Topic, course.topic_id)
    if not topic or topic.status != "active":
        await callback.answer("Linked topic is not active.", show_alert=True)
        return

    await state.update_data(
        group_id=course.group_id,
        course_id=course.id,
        topic_id=topic.id,
        target_type="targeted",
        target_topic_ids=[topic.id],
        target_names=[topic.topic_name],
    )
    await state.set_state(AnnouncementStates.waiting_content)
    await callback.message.edit_text(
        f"<b>Targeting</b>\n{html.escape(topic.topic_name)}\n\nSend the message to post.",
        reply_markup=menus.cancel_button(),
        parse_mode="HTML",
    )
    await callback.answer("Course selected.")


@router.message(AnnouncementStates.waiting_content, F.chat.type == "private")
async def receive_content(message: types.Message, state: FSMContext) -> None:
    """Store the message ID to copy/send later and show a target preview."""
    await state.update_data(
        message_id=message.message_id,
        source_chat_id=message.chat.id,
        content_text=message.text or message.caption or "",
        has_media=bool(
            message.photo
            or message.document
            or message.video
            or message.voice
            or message.audio
            or message.animation
            or message.sticker
        ),
    )
    data = await state.get_data()
    target_type = data["target_type"]
    action = "announcement" if target_type == "announcement" else ("raw broadcast" if target_type == "broadcast" else "push")
    target_names = data.get("target_names") or ["Selected topic"]
    preview = "\n".join(f"• {html.escape(name)}" for name in target_names)

    await state.set_state(AnnouncementStates.confirm)
    await message.answer(
        f"✅ Message received.\n\n<b>Targets</b>\n{preview}\n\n"
        f"Confirm {action} send?",
        reply_markup=menus.confirm_action("send_push"),
        parse_mode="HTML",
    )


@router.callback_query(AnnouncementStates.confirm, F.data == "confirm:send_push")
async def confirm_send_push(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    """Send/copy the message to all resolved target topics."""
    data = await state.get_data()
    topics = await _resolve_announcement_targets(session, data)
    if not topics:
        await callback.message.edit_text(
            "No valid target topics found.",
            reply_markup=menus.back_button(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    msg_id = data["message_id"]
    source_chat_id = data.get("source_chat_id", callback.message.chat.id)
    content_text = data.get("content_text", "")
    has_media = data.get("has_media", False)
    target_type = data.get("target_type")

    try:
        for topic in topics:
            thread_id = topic.message_thread_id if topic.message_thread_id and topic.message_thread_id > 0 else None
            if content_text and not has_media:
                text = format_announcement_html(content_text)
                await bot.send_message(
                    chat_id=topic.chat_id,
                    text=text,
                    message_thread_id=thread_id,
                    parse_mode="HTML",
                )
            else:
                kwargs = {
                    "chat_id": topic.chat_id,
                    "from_chat_id": source_chat_id,
                    "message_id": msg_id,
                }
                if thread_id:
                    kwargs["message_thread_id"] = thread_id
                await bot.copy_message(**kwargs)

        await crud.log_action(
            session,
            action="announcement_sent",
            telegram_user_id=callback.from_user.id,
            details=f"topics={','.join(str(topic.id) for topic in topics)} msg_id={msg_id}",
        )
        await state.clear()
        await callback.message.edit_text(
            f"✅ <b>Message successfully sent</b>\n\nTargets: <code>{len(topics)}</code>",
            reply_markup=menus.back_button(),
            parse_mode="HTML",
        )
        await callback.answer("Sent.")
    except Exception as exc:
        logger.error("push_failed", error=str(exc), target_topic_ids=[topic.id for topic in topics])
        await callback.message.edit_text(
            f"<b>Failed to send message:</b>\n<code>{html.escape(str(exc)[:150])}</code>",
            reply_markup=menus.back_button(),
            parse_mode="HTML",
        )
        await callback.answer("Send failed.", show_alert=True)


async def _resolve_announcement_targets(session: AsyncSession, data: dict) -> list[Topic]:
    """Resolve announcement/broadcast state into concrete Telegram topics."""
    if data.get("target_type") == "targeted" and data.get("topic_id"):
        topic = await crud.get_by_id(session, Topic, data["topic_id"])
        return [topic] if topic else []

    if data.get("target_topic_ids"):
        topics = []
        for topic_id in data["target_topic_ids"]:
            topic = await crud.get_by_id(session, Topic, topic_id)
            if topic and topic.status == "active":
                topics.append(topic)
        return topics

    if data.get("selected_course_ids"):
        topics = []
        for course_id in data["selected_course_ids"]:
            course = await crud.get_by_id(session, Course, course_id)
            if course and course.topic_id:
                topic = await crud.get_by_id(session, Topic, course.topic_id)
                if topic and topic.status == "active":
                    topics.append(topic)
        return topics

    group_id = data.get("group_id")
    if data.get("target_scope") == "general":
        general = await crud.get_general_topic(session, group_id)
        return [general] if general else []

    if data.get("target_scope") == "global":
        topics = await crud.get_active_topics(session, group_id)
        return [topic for topic in topics if topic.topic_type != "ignored"]

    return []


@router.callback_query(F.data == "menu:exam_coverage")
async def start_exam_coverage(
    callback: types.CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Begin manual exam coverage entry."""
    await state.clear()
    managed_groups = await crud.get_managed_groups(session, callback.from_user.id)
    if not managed_groups:
        await callback.message.edit_text(
            "You do not manage any registered groups.",
            reply_markup=menus.back_button(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if len(managed_groups) == 1:
        await _start_exam_coverage_for_group(callback, state, session, managed_groups[0].id)
    else:
        await state.set_state(ExamCoverageStates.waiting_group_select)
        await callback.message.edit_text(
            "<b>Select the group for this exam coverage entry:</b>",
            reply_markup=menus.group_select(managed_groups),
            parse_mode="HTML",
        )
        await callback.answer()


@router.callback_query(ExamCoverageStates.waiting_group_select, F.data.startswith("group:"))
async def cb_exam_coverage_group(
    callback: types.CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await _start_exam_coverage_for_group(callback, state, session, int(callback.data.split(":")[1]))


async def _start_exam_coverage_for_group(
    callback: types.CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    group_id: int,
) -> None:
    courses = await crud.get_active_courses(session, group_id)
    if not courses:
        await callback.message.edit_text(
            "No active courses found in this group yet.",
            reply_markup=menus.back_button(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await state.update_data(group_id=group_id)
    await state.set_state(ExamCoverageStates.waiting_course)
    await callback.message.edit_text(
        "<b>Select the course for this coverage update:</b>",
        reply_markup=menus.course_select(courses),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(ExamCoverageStates.waiting_course, F.data.startswith("course:"))
async def cb_exam_coverage_course(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    course_id = int(callback.data.split(":")[1])
    course = await crud.get_by_id(session, Course, course_id)
    if not course:
        await callback.answer("Course not found.", show_alert=True)
        return
    await state.update_data(course_id=course_id, course_name=course.course_name)
    await state.set_state(ExamCoverageStates.waiting_exam_type)
    await callback.message.edit_text(
        f"<b>{html.escape(course.course_name)}</b>\n\nSelect the exam type:",
        reply_markup=menus.exam_type_select(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(ExamCoverageStates.waiting_exam_type, F.data.startswith("exam_type:"))
async def cb_exam_coverage_type(callback: types.CallbackQuery, state: FSMContext) -> None:
    exam_type = callback.data.split(":", 1)[1]
    if exam_type == "custom":
        exam_type = "custom exam"
    await state.update_data(exam_type=exam_type)
    await state.set_state(ExamCoverageStates.waiting_chapters)
    await callback.message.edit_text(
        "Send the coverage details.\n\nExamples:\n• covers chapter 1-4\n• excluding recursion",
        reply_markup=menus.cancel_button(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ExamCoverageStates.waiting_chapters, F.chat.type == "private")
async def msg_exam_coverage_chapters(message: types.Message, state: FSMContext) -> None:
    await state.update_data(coverage_text=message.text.strip())
    await state.set_state(ExamCoverageStates.waiting_notes)
    await message.answer(
        "Add optional notes for students, or type <code>skip</code>.",
        reply_markup=menus.cancel_button(),
        parse_mode="HTML",
    )


@router.message(ExamCoverageStates.waiting_notes, F.chat.type == "private")
async def msg_exam_coverage_notes(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    notes = None if message.text.strip().lower() == "skip" else message.text.strip()
    data = await state.get_data()
    course = await crud.get_by_id(session, Course, data["course_id"])
    exam_type = data["exam_type"].replace("_", " ").title()
    coverage_text = data["coverage_text"]
    summary = (
        f"<b>{html.escape(course.course_name if course else 'Course')} {html.escape(exam_type)} Coverage</b>\n\n"
        f"{html.escape(coverage_text)}"
    )
    if notes:
        summary += f"\n\n<i>{html.escape(notes)}</i>"
    summary += "\n\nConfirm posting this coverage update?"
    await state.update_data(notes=notes)
    await state.set_state(ExamCoverageStates.confirm_post)
    await message.answer(summary, reply_markup=menus.confirm_action("exam_coverage_post"), parse_mode="HTML")


@router.callback_query(ExamCoverageStates.confirm_post, F.data == "confirm:exam_coverage_post")
async def cb_exam_coverage_confirm(
    callback: types.CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    group = await crud.get_by_id(session, Group, data["group_id"])
    course = await crud.get_by_id(session, Course, data["course_id"])
    topic = await crud.get_by_id(session, Topic, course.topic_id) if course and course.topic_id else None

    if not group or not course:
        await callback.answer("Missing course context.", show_alert=True)
        return

    item = await create_exam_coverage_entry(
        session,
        group=group,
        course=course,
        topic=topic,
        exam_type=data["exam_type"],
        coverage_text=data["coverage_text"],
        notes=data.get("notes"),
        created_by=callback.from_user.id,
    )
    await state.clear()
    await callback.message.edit_text(
        f"✅ <b>Coverage saved</b>\n\n"
        f"{html.escape(course.course_name)} coverage is now recorded and visible in Events.\n"
        f"<code>item_id={item.id}</code>",
        reply_markup=menus.back_button(),
        parse_mode="HTML",
    )
    await callback.answer("Coverage saved.")
