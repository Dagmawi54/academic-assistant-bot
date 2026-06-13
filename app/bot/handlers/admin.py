"""Admin DM handler — FSM-driven configuration wizards."""

from aiogram import Router, types, F, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import any_state
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import menus
from app.admin.states import SetupGroupStates, AddCourseStates, SemesterStates
from app.database import crud
from app.database.models import Group, Course, Topic, User
from app.logging import get_logger
import html

logger = get_logger("admin_handler")

router = Router(name="admin")


# =====================================================================
# SETUP GROUP WIZARD
# =====================================================================


@router.callback_query(F.data == "menu:setup_group")
async def start_setup_group(
    callback: types.CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Begin group setup — ask for group chat ID or forward a message from the group."""
    await state.set_state(SetupGroupStates.waiting_department)
    await callback.message.edit_text(
        "📋 <b>Group Setup</b>\n\n"
        "First, forward any message from the group you want to set up, "
        "or send the group chat ID.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SetupGroupStates.waiting_department, F.chat.type == "private")
async def setup_receive_chat_id(message: types.Message, state: FSMContext, bot: Bot) -> None:
    """Receive group reference, then ask for department."""
    chat_id = None
    if message.forward_from_chat and message.forward_from_chat.id:
        chat_id = message.forward_from_chat.id
    elif message.text and message.text.lstrip("-").isdigit():
        chat_id = int(message.text)
        if chat_id > 0:
            chat_id = int(f"-100{chat_id}")
        elif chat_id < 0 and not str(chat_id).startswith("-100"):
            chat_id = int(f"-100{abs(chat_id)}")

    if chat_id is not None:
        try:
            bot_member = await bot.get_chat_member(chat_id, bot.id)
            if bot_member.status in ("left", "kicked"):
                await message.answer("⚠️ I need to be a member of that group before you can set it up. Please add me first.", parse_mode=None)
                return
        except Exception:
            await message.answer("⚠️ Could not verify group. Ensure the ID is correct and I have been added to the chat.", parse_mode=None)
            return

        await state.update_data(chat_id=chat_id)
        await message.answer(
            f"✅ Group recognized: <code>{chat_id}</code>\n\nSelect a <b>department</b> or type a custom one:",
            reply_markup=menus.department_select(),
            parse_mode="HTML"
        )
        return

    # If we already have chat_id, this is a custom department text input
    data = await state.get_data()
    if "chat_id" not in data:
        await message.answer(
            "Please forward a message from the target group or send the chat ID first."
        )
        return
    await state.update_data(department=message.text)
    await state.set_state(SetupGroupStates.waiting_year)
    await message.answer("📅 Select the academic year:", reply_markup=menus.year_select())
    return


@router.callback_query(SetupGroupStates.waiting_department, F.data.startswith("dept:"))
async def setup_receive_dept_cb(callback: types.CallbackQuery, state: FSMContext) -> None:
    dept = callback.data.split(":")[1]
    if dept == "custom":
        await callback.message.edit_text("✏️ Type your custom department name:")
    else:
        await state.update_data(department=dept)
        await state.set_state(SetupGroupStates.waiting_year)
        await callback.message.edit_text(
            f"Department <b>{html.escape(dept)}</b> selected.\n\n📅 Select the academic year:", 
            reply_markup=menus.year_select(),
            parse_mode="HTML"
        )
    await callback.answer()


@router.callback_query(SetupGroupStates.waiting_year, F.data.startswith("year:"))
async def setup_receive_year(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Receive year selection."""
    year = int(callback.data.split(":")[1])
    await state.update_data(year=year)
    await state.set_state(SetupGroupStates.waiting_section)
    await callback.message.edit_text(
        f"Year <b>{year}</b> selected.\n\nNow enter the <b>section</b> (A, B, 1, 2, etc.):",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(SetupGroupStates.waiting_section, F.chat.type == "private")
async def setup_receive_section(message: types.Message, state: FSMContext) -> None:
    """Receive section, then ask for semester."""
    section = message.text.strip().upper()
    await state.update_data(section=section)
    await state.set_state(SetupGroupStates.waiting_semester)
    await message.answer(
        f"Section <b>{html.escape(section)}</b> set.\n\nSelect the current semester:",
        reply_markup=menus.semester_select(),
        parse_mode="HTML"
    )


@router.callback_query(SetupGroupStates.waiting_semester, F.data.startswith("semester:"))
async def setup_receive_semester(
    callback: types.CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Receive semester and create/update the group record."""
    semester = int(callback.data.split(":")[1])
    data = await state.get_data()

    chat_id = data["chat_id"]
    department = data.get("department", "Unknown")
    year = data.get("year", 1)
    section = data.get("section", "A")

    # Create or update group
    existing = await crud.get_group_by_chat_id(session, chat_id)
    user_id = callback.from_user.id
    
    if existing:
        user = await crud.get_user(session, user_id, existing.id)
        has_permission = bool(user and user.role in ("owner", "dept_admin"))
        
        if not has_permission:
            try:
                member = await callback.bot.get_chat_member(chat_id, user_id)
                if member.status in ("creator", "administrator"):
                    has_permission = True
                    if not user:
                        await crud.create(
                            session,
                            User(
                                telegram_user_id=user_id,
                                group_id=existing.id,
                                role="dept_admin",
                                username=callback.from_user.username,
                                full_name=callback.from_user.full_name,
                            )
                        )
                    else:
                        await crud.update_fields(session, User, user.id, role="dept_admin")
            except Exception:
                pass
                
        if not has_permission:
            await callback.message.edit_text("❌ This group is already managed by someone else, and you lack administrative permissions to overwrite its settings.", reply_markup=menus.back_button())
            await callback.answer()
            return
            
        await crud.update_fields(
            session,
            Group,
            existing.id,
            department=department,
            year=year,
            section=section,
            semester=semester,
        )
        group_id = existing.id
    else:
        group = Group(
            chat_id=chat_id, department=department, year=year, section=section, semester=semester
        )
        group = await crud.create(session, group)
        group_id = group.id

        await crud.create(
            session,
            User(
                telegram_user_id=user_id,
                group_id=group_id,
                role="owner",
                username=callback.from_user.username,
                full_name=callback.from_user.full_name,
            ),
        )

    await crud.log_action(
        session,
        action="group_setup",
        telegram_user_id=user_id,
        chat_id=chat_id,
        details=f"dept={department} year={year} sec={section} sem={semester}",
    )

    await state.clear()
    await callback.message.edit_text(
        f"✅ <b>Group configured!</b>\n\n"
        f"<code>Department</code> {html.escape(department)}\n"
        f"<code>Year</code> {year}\n"
        f"<code>Section</code> {html.escape(section)}\n"
        f"<code>Semester</code> {semester}\n\n"
        f"Next, add courses and link topics.",
        reply_markup=menus.back_button(),
        parse_mode="HTML"
    )
    await callback.answer()


# =====================================================================
# ADD COURSE WIZARD
# =====================================================================


@router.callback_query(F.data == "menu:add_course")
async def start_add_course(
    callback: types.CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Begin course creation."""
    users = await crud.get_user_any_group(session, callback.from_user.id)
    admin_groups = [u.group_id for u in users if u.role in {"owner", "dept_admin", "section_admin"}]

    if not admin_groups:
        await callback.message.edit_text("⚠️ No groups found.", reply_markup=menus.back_button())
        await callback.answer()
        return

    groups = []
    for gid in admin_groups:
        g = await crud.get_by_id(session, Group, gid)
        if g:
            groups.append(g)

    if len(groups) == 1:
        await state.update_data(group_id=groups[0].id)
        await state.set_state(AddCourseStates.waiting_course_name)
        await callback.message.edit_text("📚 Enter the *course name*:")
    else:
        await state.set_state(AddCourseStates.waiting_group_select)
        await callback.message.edit_text(
            "📚 Select the group:", reply_markup=menus.group_select(groups)
        )
    await callback.answer()


@router.callback_query(AddCourseStates.waiting_group_select, F.data.startswith("group:"))
async def course_group_selected(callback: types.CallbackQuery, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[1])
    await state.update_data(group_id=group_id)
    await state.set_state(AddCourseStates.waiting_course_name)
    await callback.message.edit_text("📚 Enter the <b>course name</b>:", parse_mode="HTML")
    await callback.answer()


@router.message(AddCourseStates.waiting_course_name, F.chat.type == "private")
async def course_name_received(
    message: types.Message, state: FSMContext, session: AsyncSession
) -> None:
    """Receive course name and show topic selection."""
    course_name = message.text.strip()
    data = await state.get_data()
    group_id = data["group_id"]

    await state.update_data(course_name=course_name)

    # Fetch active topics for linking
    topics = await crud.get_active_topics(session, group_id)
    if not topics:
        # No topics available — create course without linking, offer add another
        group = await crud.get_by_id(session, Group, group_id)
        course = Course(
            group_id=group_id,
            course_name=course_name,
            semester=group.semester or 1,
        )
        await crud.create(session, course)

        await crud.log_action(
            session,
            action="course_created",
            telegram_user_id=message.from_user.id,
            details=f"course={course_name} (no topics available)",
        )

        await state.set_state(AddCourseStates.waiting_course_name)
        await message.answer(
            f"✅ Course <b>{html.escape(course_name)}</b> created!\n"
            f"(No forum topics found to link)\n\n"
            f"📚 Enter another <b>course name</b> or press Cancel:",
            reply_markup=menus.cancel_only(),
            parse_mode="HTML",
        )
    else:
        await state.set_state(AddCourseStates.waiting_topic_select)
        await message.answer(
            f"🔗 Link <b>{html.escape(course_name)}</b> to a forum topic:",
            reply_markup=menus.topic_select_with_skip(topics),
            parse_mode="HTML",
        )


@router.callback_query(AddCourseStates.waiting_topic_select, F.data.startswith("topic:"))
async def course_topic_selected(
    callback: types.CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Link course to selected topic and create."""
    topic_id = int(callback.data.split(":")[1])
    data = await state.get_data()

    group = await crud.get_by_id(session, Group, data["group_id"])
    course = Course(
        group_id=data["group_id"],
        course_name=data["course_name"],
        semester=group.semester or 1,
        topic_id=topic_id,
    )
    course = await crud.create(session, course)

    # Mark topic as course type
    await crud.update_fields(session, Topic, topic_id, topic_type="course")

    await crud.log_action(
        session,
        action="course_created",
        telegram_user_id=callback.from_user.id,
        details=f"course={data['course_name']} topic_id={topic_id}",
    )

    # Offer to add another course
    await state.set_state(AddCourseStates.waiting_course_name)
    await callback.message.edit_text(
        f"✅ Course <b>{html.escape(data['course_name'])}</b> created and linked!\n\n"
        f"📚 Enter another <b>course name</b> or press Cancel:",
        reply_markup=menus.cancel_only(),
    )
    await callback.answer()


@router.callback_query(AddCourseStates.waiting_topic_select, F.data == "skip_topic")
async def course_skip_topic(
    callback: types.CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Create course without linking a topic."""
    data = await state.get_data()
    group = await crud.get_by_id(session, Group, data["group_id"])

    course = Course(
        group_id=data["group_id"],
        course_name=data["course_name"],
        semester=group.semester or 1,
    )
    await crud.create(session, course)

    await crud.log_action(
        session,
        action="course_created",
        telegram_user_id=callback.from_user.id,
        details=f"course={data['course_name']} (skipped topic)",
    )

    # Offer to add another course
    await state.set_state(AddCourseStates.waiting_course_name)
    await callback.message.edit_text(
        f"✅ Course <b>{html.escape(data['course_name'])}</b> created (no topic linked)!\n\n"
        f"📚 Enter another <b>course name</b> or press Cancel:",
        reply_markup=menus.cancel_only(),
    )
    await callback.answer()


@router.callback_query(F.data == "done_adding_courses", StateFilter(any_state))
async def cb_done_adding_courses(callback: types.CallbackQuery, state: FSMContext) -> None:
    """End the add course loop gracefully."""
    await state.clear()
    await callback.message.edit_text(
        "✅ Courses saved. Use <code>/menu</code> to configure more.",
        reply_markup=menus.back_button(),
    )
    await callback.answer()


# =====================================================================
# SEMESTER CONTROL
# =====================================================================


@router.callback_query(F.data == "menu:semester")
async def start_semester(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SemesterStates.waiting_action)
    await callback.message.edit_text("📅 <b>Semester Control</b>", reply_markup=menus.semester_actions())
    await callback.answer()


@router.callback_query(SemesterStates.waiting_action, F.data == "sem:close")
async def semester_confirm_close(
    callback: types.CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Ask for confirmation before closing semester."""
    users = await crud.get_user_any_group(session, callback.from_user.id)
    admin_groups = [u.group_id for u in users if u.role in {"owner", "dept_admin", "section_admin"}]

    if not admin_groups:
        await callback.message.edit_text("⚠️ No groups found.", reply_markup=menus.back_button())
        await callback.answer()
        return

    # For simplicity, use the first admin group (extend later for multi-group)
    group = await crud.get_by_id(session, Group, admin_groups[0])
    await state.update_data(group_id=group.id)
    await state.set_state(SemesterStates.confirm_close)

    await callback.message.edit_text(
        f"⚠️ Close semester <b>{group.semester}</b> for "
        f"<b>{html.escape(group.department or '')}</b> Y{group.year} S{html.escape(group.section or '')}?\n\n"
        "This will:\n"
        "• Close all current course topics\n"
        "• Cancel pending reminders\n"
        "• Deactivate current courses",
        reply_markup=menus.confirm_action("close_semester"),
    )
    await callback.answer()


@router.callback_query(SemesterStates.confirm_close, F.data.startswith("confirm:"))
async def semester_do_close(
    callback: types.CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Execute semester close."""
    data = await state.get_data()
    group_id = data["group_id"]

    from app.services.semester_service import close_semester

    await close_semester(session, group_id)

    await crud.log_action(
        session,
        action="semester_closed",
        telegram_user_id=callback.from_user.id,
        details=f"group_id={group_id}",
    )

    await state.clear()
    await callback.message.edit_text(
        "🔒 <b>Semester closed!</b>\n\n"
        "All topics closed, courses deactivated, reminders cancelled.\n"
        "Use the menu to set up a new semester.",
        reply_markup=menus.back_button(),
    )
    await callback.answer()


@router.callback_query(F.data.in_({
    "menu:view_dms"
}))
async def placeholder_menu(callback: types.CallbackQuery) -> None:
    """Placeholder for features to be implemented."""
    await callback.message.edit_text(
        "🚧 This feature is coming soon!",
        reply_markup=menus.back_button(),
    )
    await callback.answer()

# =====================================================================
# CATEGORY MENUS
# =====================================================================

@router.callback_query(F.data.in_({"menu:cat_infrastructure", "menu:cat_courses"}))
async def cb_cat_infrastructure(callback: types.CallbackQuery) -> None:
    await callback.message.edit_text(
        "📚 <b>Courses</b>", reply_markup=menus.cat_courses(), parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "menu:cat_communications")
async def cb_cat_communications(callback: types.CallbackQuery) -> None:
    await callback.message.edit_text(
        "📢 <b>Communications</b>", reply_markup=menus.cat_communications(), parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "menu:cat_administration")
async def cb_cat_administration(callback: types.CallbackQuery) -> None:
    await callback.message.edit_text(
        "⚙️ <b>Administration Settings</b>",
        reply_markup=menus.cat_administration(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "menu:cat_analytics")
async def cb_cat_analytics(callback: types.CallbackQuery) -> None:
    await callback.message.edit_text(
        "📊 <b>Analytics & Logs</b>\n\nOperational visibility for events, reminders, duplicates, and admin activity.",
        reply_markup=menus.cat_analytics(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "menu:analytics_overview")
async def cb_menu_analytics_overview(callback: types.CallbackQuery, session: AsyncSession) -> None:
    from sqlalchemy import func, select
    from app.database.models import AcademicItem, DuplicateLog, Reminder

    groups = await crud.get_managed_groups(session, callback.from_user.id)
    if not groups:
        await callback.message.edit_text("❌ You don't manage any groups.", reply_markup=menus.back_button())
        await callback.answer()
        return

    group = groups[0]
    item_count = await session.scalar(select(func.count()).select_from(AcademicItem).where(AcademicItem.group_id == group.id))
    low_conf = await session.scalar(select(func.count()).select_from(AcademicItem).where(AcademicItem.group_id == group.id, AcademicItem.status == "new"))
    dup_count = await session.scalar(select(func.count()).select_from(DuplicateLog).where(DuplicateLog.group_id == group.id))
    reminder_count = await session.scalar(
        select(func.count())
        .select_from(Reminder)
        .join(AcademicItem, AcademicItem.id == Reminder.item_id)
        .where(AcademicItem.group_id == group.id, Reminder.sent == False, Reminder.cancelled == False)  # noqa: E712
    )

    text = (
        f"📊 <b>Analytics Overview</b>\n\n"
        f"<b>Group</b>: {html.escape(group.department or 'Group')}\n"
        f"<b>Detected items</b>: <code>{item_count or 0}</code>\n"
        f"<b>Low confidence</b>: <code>{low_conf or 0}</code>\n"
        f"<b>Pending reminders</b>: <code>{reminder_count or 0}</code>\n"
        f"<b>Suppressed duplicates</b>: <code>{dup_count or 0}</code>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=menus.cat_analytics())
    await callback.answer()


@router.callback_query(F.data == "menu:logs_recent")
async def cb_menu_logs_recent(callback: types.CallbackQuery, session: AsyncSession) -> None:
    from sqlalchemy import select
    from app.database.models import AcademicItem, DuplicateLog

    groups = await crud.get_managed_groups(session, callback.from_user.id)
    if not groups:
        await callback.message.edit_text("❌ You don't manage any groups.", reply_markup=menus.back_button())
        await callback.answer()
        return

    group = groups[0]
    items = (
        await session.execute(
            select(AcademicItem)
            .where(AcademicItem.group_id == group.id)
            .order_by(AcademicItem.created_at.desc())
            .limit(5)
        )
    ).scalars().all()
    duplicates = (
        await session.execute(
            select(DuplicateLog)
            .where(DuplicateLog.group_id == group.id)
            .order_by(DuplicateLog.created_at.desc())
            .limit(5)
        )
    ).scalars().all()

    lines = ["🧾 <b>Recent Runtime Logs</b>", ""]
    if items:
        lines.append("<b>Latest detections</b>")
        for item in items:
            lines.append(
                f"• <code>{item.created_at.strftime('%m-%d %H:%M')}</code> {html.escape(item.item_type)} — {html.escape(item.title or 'Untitled')}"
            )
        lines.append("")
    if duplicates:
        lines.append("<b>Latest suppressions</b>")
        for log in duplicates:
            lines.append(
                f"• <code>{log.created_at.strftime('%m-%d %H:%M')}</code> item {log.existing_item_id} — {html.escape(log.reason or 'No reason')}"
            )
    if len(lines) == 2:
        lines.append("No routing or suppression records yet.")

    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=menus.cat_analytics())
    await callback.answer()


@router.callback_query(F.data == "menu:permissions")
async def cb_menu_permissions(callback: types.CallbackQuery) -> None:
    text = (
        "👥 <b>Managing Permissions</b>\n\n"
        "To promote or demote a user, perform these steps inside the group:\n"
        "1. Go to the target group.\n"
        "2. Reply to the user's message.\n"
        "3. Type <code>/promote <role></code> or <code>/demote</code>.\n\n"
        "<b>Available Roles:</b>\n"
        "<code>dept_admin</code>, <code>section_admin</code>, <code>moderator</code>"
    )
    await callback.message.edit_text(
        text, reply_markup=menus.back_button(), parse_mode="HTML"
    )
    await callback.answer()




from aiogram.filters import Command
from app.admin.permissions import require_role


@router.message(Command("review"))
@require_role(["creator", "administrator", "dept_admin"])
async def cmd_review_items(message: types.Message, session: AsyncSession) -> None:
    """Admin command to review low-confidence academic items."""
    group_id = message.chat.id
    from sqlalchemy import select
    from app.database.models import AcademicItem

    # Query items that need review
    stmt = (
        select(AcademicItem)
        .where(AcademicItem.group_id == group_id, AcademicItem.status == "new")
        .limit(5)
    )

    result = await session.execute(stmt)
    pending_items = result.scalars().all()

    if not pending_items:
        await message.answer("✅ No pending items require review.")
        return

    for item in pending_items:
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Approve", callback_data=f"review_approve:{item.id}")
        kb.button(text="❌ Reject", callback_data=f"review_reject:{item.id}")

        text = (
            f"🔍 <b>Pending Review</b>\n"
            f"Type: {item.item_type.upper()}\n"
            f"Title: {item.title}\n"
            f"Confidence: {item.confidence:.2f}\n"
            f"Deadline: {item.deadline}\n"
            f"Text snippet: {item.raw_text[:100]}..."
        )
        await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("review_"))
async def callback_review_action(query: types.CallbackQuery, session: AsyncSession) -> None:
    """Handle approve/reject actions for pending items."""
    action, item_id_str = query.data.split(":")
    item_id = int(item_id_str)

    from app.database.models import AcademicItem

    item = await crud.get_by_id(session, AcademicItem, item_id)

    if not item:
        await query.answer("Item not found.")
        return

    from app.metrics.tracker import tracker

    if action == "review_approve":
        await tracker.record_admin_review(approved=True)
        await crud.update_fields(session, AcademicItem, item.id, status="active")
        await query.message.edit_text(f"✅ Approved item: {item.title}")

        # Trigger reminders now that it's active
        from app.events.bus import emit, ASSIGNMENT_DETECTED, EXAM_DETECTED

        event = ASSIGNMENT_DETECTED if item.item_type == "assignment" else EXAM_DETECTED
        await emit(event, item_id=item.id)

    elif action == "review_reject":
        await tracker.record_admin_review(approved=False)
        await crud.update_fields(session, AcademicItem, item.id, status="rejected")
        await query.message.edit_text(f"❌ Rejected item: {item.title}")

    await query.answer()


@router.callback_query(F.data == "menu:metrics")
async def cb_menu_metrics(callback: types.CallbackQuery) -> None:
    from app.metrics.tracker import tracker

    report = await tracker.get_report()
    lines = [
        "🚀 <b>Academic Assistant Bot v1.1.0</b>",
        "",
        "📊 <b>System Metrics</b>:"
    ]
    for k, v in report.items():
        lines.append(f"• {html.escape(k)}: {html.escape(str(v))}")
    
    lines.extend([
        "",
        "<b>Recent Updates:</b>",
        "• OCR Image Extraction",
        "• Document Extraction (PDF/DOCX)",
        "• Voice Note Transcription",
        "• Duplicate Semantic Detection",
        "• Advanced Role Management & Audit Logs"
    ])

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=menus.back_button()
    )
    await callback.answer()


@router.message(Command("promote"))
async def cmd_promote(message: types.Message, session: AsyncSession) -> None:
    """Promote a user in the group from student to moderator/admin."""
    if message.chat.type not in ("group", "supergroup"):
        return

    from app.admin.permissions import has_role, ROLE_HIERARCHY

    if not await has_role(session, message.from_user.id, message.chat.id, "dept_admin"):
        await message.answer("❌ Only Dept Admins and Owners can promote users.")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer(
            "⚠️ Reply to a user's message to promote them.\nUsage: <code>/promote <role></code>",
            parse_mode="HTML",
        )
        return

    target_user = message.reply_to_message.from_user
    args = message.text.split()
    role = args[1].lower() if len(args) > 1 else "moderator"

    if role not in ROLE_HIERARCHY:
        valid = ", ".join(ROLE_HIERARCHY.keys())
        await message.answer(f"⚠️ Invalid role. Valid roles: {valid}")
        return

    if role == "owner" and not await has_role(
        session, message.from_user.id, message.chat.id, "owner"
    ):
        await message.answer("❌ Only Owners can grant the Owner role.")
        return

    from app.database.models import User

    group = await crud.get_group_by_chat_id(session, message.chat.id)
    if not group:
        await message.answer("❌ This group is not registered.")
        return

    existing = await crud.get_user(session, target_user.id, group.id)
    if existing:
        await crud.update_fields(session, User, existing.id, role=role)
    else:
        await crud.create(
            session,
            User(
                telegram_user_id=target_user.id,
                group_id=group.id,
                role=role,
                username=target_user.username,
                full_name=target_user.full_name,
            ),
        )

    await crud.log_action(
        session,
        "promoted_user",
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        details=f"promoted={target_user.id} role={role}",
    )
    await message.answer(
        f"✅ User {target_user.full_name} has been promoted to <code>{role}</code>.", parse_mode="HTML"
    )


@router.message(Command("demote"))
async def cmd_demote(message: types.Message, session: AsyncSession) -> None:
    """Demote a user back to student."""
    if message.chat.type not in ("group", "supergroup"):
        return

    from app.admin.permissions import has_role

    if not await has_role(session, message.from_user.id, message.chat.id, "dept_admin"):
        await message.answer("❌ Only Dept Admins and Owners can demote users.")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("⚠️ Reply to a user's message to demote them.")
        return

    target_user = message.reply_to_message.from_user

    from app.database.models import User

    group = await crud.get_group_by_chat_id(session, message.chat.id)
    if not group:
        await message.answer("❌ This group is not registered.")
        return

    existing = await crud.get_user(session, target_user.id, group.id)
    if existing:
        await crud.update_fields(session, User, existing.id, role="student")
        await crud.log_action(
            session,
            "demoted_user",
            telegram_user_id=message.from_user.id,
            chat_id=message.chat.id,
            details=f"demoted={target_user.id}",
        )
        await message.answer(
            f"✅ User {target_user.full_name} has been demoted to <code>student</code>.", parse_mode="HTML"
        )
    else:
        await message.answer(f"⚠️ User {target_user.full_name} is already a student/unregistered.")


@router.message(Command("sync_admin"))
async def cmd_sync_admin(message: types.Message, session: AsyncSession) -> None:
    """Sync Telegram native admin status to internal DB. Auto-creates group if needed."""
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("❌ This command must be used inside a group.")
        return

    chat_member = await message.chat.get_member(message.from_user.id)
    if not chat_member or chat_member.status not in ("creator", "administrator"):
        await message.answer("❌ You are not a Telegram administrator in this group.")
        return

    # Auto-create the group record if it doesn't exist yet
    group = await crud.get_group_by_chat_id(session, message.chat.id)
    if not group:
        group = Group(
            chat_id=message.chat.id,
            active=True,
        )
        group = await crud.create(session, group)
        await message.answer(
            f"📋 Group registered (chat_id: <code>{message.chat.id}</code>)\n"
            f"Use <code>/menu</code> in my DMs to configure department, year, and section.",
            parse_mode="HTML",
        )

    role = "owner" if chat_member.status == "creator" else "dept_admin"
    from app.database.models import User
    
    existing = await crud.get_user(session, message.from_user.id, group.id)
    if existing:
        if existing.role in ("student", "moderator", "representative"):
            await crud.update_fields(session, User, existing.id, role=role)
            await message.answer(f"✅ Your role has been updated to <code>{role}</code>.", parse_mode="HTML")
        else:
            await message.answer(f"✅ You are already synced as <code>{existing.role}</code>.", parse_mode="HTML")
    else:
        await crud.create(
            session,
            User(
                telegram_user_id=message.from_user.id,
                group_id=group.id,
                role=role,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            ),
        )
        await message.answer(f"✅ Registered as <code>{role}</code>! Use <code>/menu</code> in my DMs to manage this group.", parse_mode="HTML")

    # Also register the current topic if typed from a thread
    if message.message_thread_id:
        from app.database.models import Topic
        existing_topic = await crud.get_topic(session, message.chat.id, message.message_thread_id)
        if not existing_topic:
            new_topic = Topic(
                group_id=group.id,
                chat_id=message.chat.id,
                message_thread_id=message.message_thread_id,
                topic_name=f"Topic {message.message_thread_id}",
                topic_type="ignored",
                status="active",
            )
            await crud.create(session, new_topic)
            await message.answer("📌 This topic has been registered too!", parse_mode="HTML")


@router.message(Command("scan_topics"), StateFilter(any_state))
async def cmd_scan_topics(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Register the current forum topic in the database.
    Users should type this command in each topic they want to make available for course linking.
    """
    await state.clear()
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("❌ This command must be used inside a group topic.")
        return

    group = await crud.get_group_by_chat_id(session, message.chat.id)
    if not group:
        await message.answer("❌ This group is not registered. Type <code>/sync_admin</code> first.", parse_mode="HTML")
        return

    thread_id = message.message_thread_id
    if not thread_id:
        # They're in the General topic
        from app.database.models import Topic
        general = await crud.get_general_topic(session, group.id)
        if not general:
            new_topic = Topic(
                group_id=group.id,
                chat_id=message.chat.id,
                message_thread_id=0,
                topic_name="General",
                topic_type="general",
                status="active",
            )
            await crud.create(session, new_topic)
        await message.answer("✅ General topic registered!")
        return

    from app.database.models import Topic
    existing = await crud.get_topic(session, message.chat.id, thread_id)
    if existing:
        await message.answer(
            f"✅ This topic is already registered as <code>{existing.topic_name}</code>.\n"
            f"_(Tip: You can rename it by typing <code>/scan_topics Your Custom Name</code>)_",
            parse_mode="HTML"
        )
        return

    args = message.text.split(maxsplit=1)
    topic_name = args[1].strip() if len(args) > 1 else f"Topic {thread_id}"

    new_topic = Topic(
        group_id=group.id,
        chat_id=message.chat.id,
        message_thread_id=thread_id,
        topic_name=topic_name,
        topic_type="course",
        status="active"
    )
    await crud.create(session, new_topic)
    
    msg = f"✅ Topic registered as <code>{topic_name}</code>!\nYou can now link it to a course via Add Course in <code>/menu</code>."
    if len(args) == 1:
        msg += "\n\n_(Tip: In the future, you can instantly name it by typing <code>/scan_topics Your Topic Name</code>)_"
        
    await message.answer(msg, parse_mode="HTML")


@router.callback_query(F.data == "menu:audit")
async def cb_menu_audit(callback: types.CallbackQuery, session: AsyncSession) -> None:
    groups = await crud.get_managed_groups(session, callback.from_user.id)
    if not groups:
        await callback.message.edit_text(
            "❌ You don't manage any groups.", reply_markup=menus.back_button()
        )
        return
    await callback.message.edit_text(
        "📝 Select group to view audit logs:",
        reply_markup=menus.group_select(groups, prefix="view_audit"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("view_audit:"))
async def cb_view_audit(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await crud.get_group(session, group_id)
    if group:
        from sqlalchemy import select
        from app.database.models import AuditLog

        stmt = (
            select(AuditLog)
            .where(AuditLog.chat_id == group.chat_id)
            .order_by(AuditLog.created_at.desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        logs = result.scalars().all()
        if not logs:
            await callback.message.edit_text(
                "No audit logs found.", reply_markup=menus.back_button()
            )
            return
        lines = [f"📝 <b>Audit Logs for {html.escape(group.department or 'Group')}</b>"]
        for log in logs:
            time_str = html.escape(log.created_at.strftime("%Y-%m-%d %H:%M"))
            action = html.escape(log.action)
            details = f" — {html.escape(log.details)}" if log.details else ""
            lines.append(f"• <code>{time_str}</code>: {action}{details}")
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=menus.back_button()
        )
    await callback.answer()


@router.callback_query(F.data == "menu:safety")
async def cb_menu_safety(callback: types.CallbackQuery, session: AsyncSession) -> None:
    groups = await crud.get_managed_groups(session, callback.from_user.id)
    if not groups:
        await callback.message.edit_text(
            "❌ You don't manage any groups.", reply_markup=menus.back_button()
        )
        return
    await callback.message.edit_text(
        "🛡️ Select group to toggle safety filter:",
        reply_markup=menus.group_select(groups, prefix="set_safety"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("set_safety:"))
async def cb_set_safety(callback: types.CallbackQuery, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    group = await crud.get_group(session, group_id)
    if group:
        from app.admin.permissions import has_role

        if await has_role(session, callback.from_user.id, group.chat_id, "dept_admin"):
            new_status = not getattr(group, "ai_moderation_enabled", False)
            await crud.update_fields(session, Group, group.id, ai_moderation_enabled=new_status)
            status_str = "ON 🛡️" if new_status else "OFF ⚠️"
            await callback.message.edit_text(
                f"AI Safety Filter for <b>{html.escape(group.department or 'Group')}</b> is now {status_str}.",
                parse_mode="HTML",
                reply_markup=menus.back_button(),
            )
        else:
            await callback.message.edit_text("❌ Access Denied.", reply_markup=menus.back_button())
    await callback.answer()

