"""Admin DM handler — FSM-driven configuration wizards."""

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import menus
from app.admin.states import SetupGroupStates, AddCourseStates, SemesterStates, LinkTopicStates
from app.database import crud
from app.database.models import Group, Course, Topic, User
from app.logging import get_logger
from app.utils.text import escape_md

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
        "📋 *Group Setup*\n\n"
        "First, forward any message from the group you want to set up, "
        "or send the group chat ID\\.",
    )
    await callback.answer()


@router.message(SetupGroupStates.waiting_department, F.chat.type == "private")
async def setup_receive_chat_id(message: types.Message, state: FSMContext) -> None:
    """Receive group reference, then ask for department."""
    # Check if it's a forwarded message from a group
    if message.forward_from_chat and message.forward_from_chat.id:
        chat_id = message.forward_from_chat.id
        await state.update_data(chat_id=chat_id)
    elif message.text and message.text.lstrip("-").isdigit():
        chat_id = int(message.text)
        await state.update_data(chat_id=chat_id)
    else:
        # If we already have chat_id, this is a custom department text input
        data = await state.get_data()
        if "chat_id" not in data:
            await message.answer(
                "Please forward a message from the target group or send the chat ID first\\."
            )
            return
        await state.update_data(department=message.text)
        await state.set_state(SetupGroupStates.waiting_year)
        await message.answer("📅 Select the academic year:", reply_markup=menus.year_select())
        return

    await message.answer(
        f"✅ Group registered: `{chat_id}`\n\nSelect a *department* or type a custom one:",
        reply_markup=menus.department_select()
    )


@router.callback_query(SetupGroupStates.waiting_department, F.data.startswith("dept:"))
async def setup_receive_dept_cb(callback: types.CallbackQuery, state: FSMContext) -> None:
    dept = callback.data.split(":")[1]
    if dept == "custom":
        await callback.message.edit_text("✏️ Type your custom department name:")
    else:
        await state.update_data(department=dept)
        await state.set_state(SetupGroupStates.waiting_year)
        await callback.message.edit_text(
            f"Department *{escape_md(dept)}* selected\\.\n\n📅 Select the academic year:", 
            reply_markup=menus.year_select()
        )
    await callback.answer()


@router.callback_query(SetupGroupStates.waiting_year, F.data.startswith("year:"))
async def setup_receive_year(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Receive year selection."""
    year = int(callback.data.split(":")[1])
    await state.update_data(year=year)
    await state.set_state(SetupGroupStates.waiting_section)
    await callback.message.edit_text(
        f"Year *{year}* selected\\.\n\nNow enter the *section* \\(A, B, 1, 2, etc\\.\\):"
    )
    await callback.answer()


@router.message(SetupGroupStates.waiting_section, F.chat.type == "private")
async def setup_receive_section(message: types.Message, state: FSMContext) -> None:
    """Receive section, then ask for semester."""
    section = message.text.strip().upper()
    await state.update_data(section=section)
    await state.set_state(SetupGroupStates.waiting_semester)
    await message.answer(
        f"Section *{escape_md(section)}* set\\.\n\nSelect the current semester:",
        reply_markup=menus.semester_select(),
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
    if existing:
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

    # Ensure the configuring user is registered as owner
    user_id = callback.from_user.id
    user = await crud.get_user(session, user_id, group_id)
    if not user:
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
        f"✅ *Group configured\\!*\n\n"
        f"`Department` {escape_md(department)}\n"
        f"`Year` {year}\n"
        f"`Section` {escape_md(section)}\n"
        f"`Semester` {semester}\n\n"
        f"Next, add courses and link topics\\.",
        reply_markup=menus.back_button(),
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
        await callback.message.edit_text("⚠️ No groups found\\.", reply_markup=menus.back_button())
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
    await callback.message.edit_text("📚 Enter the *course name*:")
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
        await state.set_state(AddCourseStates.confirm)
        await message.answer(
            f"Course *{escape_md(course_name)}* will be created without a linked topic\\.\n"
            f"You can link a topic later\\.",
            reply_markup=menus.confirm_action("create_course"),
        )
    else:
        await state.set_state(AddCourseStates.waiting_topic_select)
        await message.answer(
            f"Link *{escape_md(course_name)}* to a topic:",
            reply_markup=menus.topic_select(topics),
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

    await state.clear()
    await callback.message.edit_text(
        f"✅ Course *{escape_md(data['course_name'])}* created and linked\\!",
        reply_markup=menus.back_button(),
    )
    await callback.answer()


@router.callback_query(AddCourseStates.confirm, F.data.startswith("confirm:"))
async def course_confirm_no_topic(
    callback: types.CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Create course without topic link."""
    data = await state.get_data()
    group = await crud.get_by_id(session, Group, data["group_id"])

    course = Course(
        group_id=data["group_id"],
        course_name=data["course_name"],
        semester=group.semester or 1,
    )
    await crud.create(session, course)

    await state.clear()
    await callback.message.edit_text(
        f"✅ Course *{escape_md(data['course_name'])}* created \\(no topic linked\\)\\.",
        reply_markup=menus.back_button(),
    )
    await callback.answer()


# =====================================================================
# SEMESTER CONTROL
# =====================================================================


@router.callback_query(F.data == "menu:semester")
async def start_semester(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SemesterStates.waiting_action)
    await callback.message.edit_text("📅 *Semester Control*", reply_markup=menus.semester_actions())
    await callback.answer()


@router.callback_query(SemesterStates.waiting_action, F.data == "sem:close")
async def semester_confirm_close(
    callback: types.CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Ask for confirmation before closing semester."""
    users = await crud.get_user_any_group(session, callback.from_user.id)
    admin_groups = [u.group_id for u in users if u.role in {"owner", "dept_admin", "section_admin"}]

    if not admin_groups:
        await callback.message.edit_text("⚠️ No groups found\\.", reply_markup=menus.back_button())
        await callback.answer()
        return

    # For simplicity, use the first admin group (extend later for multi-group)
    group = await crud.get_by_id(session, Group, admin_groups[0])
    await state.update_data(group_id=group.id)
    await state.set_state(SemesterStates.confirm_close)

    await callback.message.edit_text(
        f"⚠️ Close semester *{group.semester}* for "
        f"*{escape_md(group.department or '')}* Y{group.year} S{escape_md(group.section or '')}\\?\n\n"
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
        "🔒 *Semester closed\\!*\n\n"
        "All topics closed, courses deactivated, reminders cancelled\\.\n"
        "Use the menu to set up a new semester\\.",
        reply_markup=menus.back_button(),
    )
    await callback.answer()


# =====================================================================
# PLACEHOLDER CALLBACKS for remaining menu items
# =====================================================================


@router.callback_query(F.data == "menu:cat_infrastructure")
async def cb_cat_infrastructure(callback: types.CallbackQuery) -> None:
    await callback.message.edit_text(
        "🏢 *Infrastructure*", reply_markup=menus.cat_infrastructure(), parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "menu:cat_communications")
async def cb_cat_communications(callback: types.CallbackQuery) -> None:
    await callback.message.edit_text(
        "📢 *Communications*", reply_markup=menus.cat_communications(), parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "menu:cat_administration")
async def cb_cat_administration(callback: types.CallbackQuery) -> None:
    await callback.message.edit_text(
        "⚙️ *Administration Settings*",
        reply_markup=menus.cat_administration(),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data == "menu:cat_analytics")
async def cb_cat_analytics(callback: types.CallbackQuery) -> None:
    await callback.message.edit_text(
        "📊 *Analytics & Logs*", reply_markup=menus.cat_analytics(), parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "menu:permissions")
async def cb_menu_permissions(callback: types.CallbackQuery) -> None:
    text = (
        "👥 *Managing Permissions*\n\n"
        "To promote or demote a user, perform these steps inside the group:\n"
        "1\\. Go to the target group\\.\n"
        "2\\. Reply to the user's message\\.\n"
        "3\\. Type `/promote <role>` or `/demote`\\.\n\n"
        "*Available Roles:*\n"
        "`dept_admin`, `section_admin`, `moderator`"
    )
    await callback.message.edit_text(
        text, reply_markup=menus.back_button(), parse_mode="MarkdownV2"
    )
    await callback.answer()


@router.callback_query(F.data.in_({"menu:exam_coverage", "menu:announcements"}))
async def placeholder_menu(callback: types.CallbackQuery) -> None:
    """Placeholder for features to be implemented."""
    await callback.message.edit_text(
        "🚧 This feature is coming soon\\!",
        reply_markup=menus.back_button(),
    )
    await callback.answer()


# =====================================================================
# LINK TOPICS WIZARD
# =====================================================================

@router.callback_query(F.data == "menu:link_topics")
async def start_link_topics(
    callback: types.CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Begin topic linking."""
    users = await crud.get_user_any_group(session, callback.from_user.id)
    admin_groups = [u.group_id for u in users if u.role in {"owner", "dept_admin", "section_admin"}]

    if not admin_groups:
        await callback.message.edit_text("⚠️ No groups found\\.", reply_markup=menus.back_button())
        await callback.answer()
        return

    groups = []
    for gid in admin_groups:
        g = await crud.get_by_id(session, Group, gid)
        if g:
            groups.append(g)

    if len(groups) == 1:
        await state.update_data(group_id=groups[0].id)
        # Fetch courses
        courses = await crud.get_active_courses(session, groups[0].id)
        if not courses:
            await callback.message.edit_text("⚠️ No active courses found in this group\\.", reply_markup=menus.back_button())
            return
        await state.set_state(LinkTopicStates.waiting_course_select)
        await callback.message.edit_text("📚 Select the *course* you want to link:", reply_markup=menus.course_select(courses))
    else:
        await state.set_state(LinkTopicStates.waiting_group_select)
        await callback.message.edit_text(
            "📚 Select the group:", reply_markup=menus.group_select(groups)
        )
    await callback.answer()

@router.callback_query(LinkTopicStates.waiting_group_select, F.data.startswith("group:"))
async def link_group_selected(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    group_id = int(callback.data.split(":")[1])
    await state.update_data(group_id=group_id)
    courses = await crud.get_active_courses(session, group_id)
    if not courses:
        await callback.message.edit_text("⚠️ No active courses found in this group\\.", reply_markup=menus.back_button())
        return
    await state.set_state(LinkTopicStates.waiting_course_select)
    await callback.message.edit_text("📚 Select the *course* you want to link:", reply_markup=menus.course_select(courses))
    await callback.answer()

@router.callback_query(LinkTopicStates.waiting_course_select, F.data.startswith("course:"))
async def link_course_selected(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    course_id = int(callback.data.split(":")[1])
    await state.update_data(course_id=course_id)
    
    data = await state.get_data()
    topics = await crud.get_active_topics(session, data["group_id"])
    if not topics:
        await callback.message.edit_text("⚠️ No active topics found in this group\\.", reply_markup=menus.back_button())
        return
    
    await state.set_state(LinkTopicStates.waiting_topic_select)
    await callback.message.edit_text("💬 Select the *topic* to link to this course:", reply_markup=menus.topic_select(topics))
    await callback.answer()

@router.callback_query(LinkTopicStates.waiting_topic_select, F.data.startswith("topic:"))
async def link_topic_selected(callback: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    topic_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    
    course_id = data["course_id"]
    from app.database.models import Course, Topic
    
    await crud.update_fields(session, Course, course_id, topic_id=topic_id)
    await crud.update_fields(session, Topic, topic_id, topic_type="course")
    
    course = await crud.get_by_id(session, Course, course_id)
    title = course.course_name if course else "Unknown"

    await crud.log_action(
        session,
        action="topic_linked",
        telegram_user_id=callback.from_user.id,
        details=f"course_id={course_id} topic_id={topic_id}"
    )
    
    await state.clear()
    await callback.message.edit_text(
        f"✅ Successfully linked course *{escape_md(title)}* to the selected topic\\!",
        reply_markup=menus.back_button()
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
            f"🔍 *Pending Review*\n"
            f"Type: {item.item_type.upper()}\n"
            f"Title: {item.title}\n"
            f"Confidence: {item.confidence:.2f}\n"
            f"Deadline: {item.deadline}\n"
            f"Text snippet: {item.raw_text[:100]}..."
        )
        await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


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
    lines = ["📊 *System Metrics*:"]
    for k, v in report.items():
        lines.append(f"• {escape_md(k)}: {escape_md(str(v))}")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="MarkdownV2", reply_markup=menus.back_button()
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
            "⚠️ Reply to a user's message to promote them.\nUsage: `/promote <role>`",
            parse_mode="Markdown",
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
        f"✅ User {target_user.full_name} has been promoted to `{role}`.", parse_mode="Markdown"
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
            f"✅ User {target_user.full_name} has been demoted to `student`.", parse_mode="Markdown"
        )
    else:
        await message.answer(f"⚠️ User {target_user.full_name} is already a student/unregistered.")


@router.message(Command("sync_admin"))
async def cmd_sync_admin(message: types.Message, session: AsyncSession) -> None:
    """Sync Telegram native admin status to internal DB."""
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("❌ This command must be used inside a group.")
        return

    chat_member = await message.chat.get_member(message.from_user.id)
    if not chat_member or chat_member.status not in ("creator", "administrator"):
        await message.answer("❌ You are not a Telegram administrator in this group.")
        return

    group = await crud.get_group_by_chat_id(session, message.chat.id)
    if not group:
        await message.answer("❌ This group is not registered.")
        return

    role = "owner" if chat_member.status == "creator" else "dept_admin"
    from app.database.models import User
    
    existing = await crud.get_user(session, message.from_user.id, group.id)
    if existing:
        if existing.role in ("student", "moderator", "representative"):
            await crud.update_fields(session, User, existing.id, role=role)
            await message.answer(f"✅ Your role has been updated to `{role}` in the database.", parse_mode="Markdown")
        else:
            await message.answer(f"✅ You are already synced as `{existing.role}`.", parse_mode="Markdown")
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
        await message.answer(f"✅ You have been successfully registered as `{role}`! You can now use `/menu` in my DMs.", parse_mode="Markdown")


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
            .order_by(AuditLog.timestamp.desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        logs = result.scalars().all()
        if not logs:
            await callback.message.edit_text(
                "No audit logs found.", reply_markup=menus.back_button()
            )
            return
        lines = [f"📝 *Audit Logs for {escape_md(group.department or 'Group')}*"]
        for log in logs:
            time_str = escape_md(log.timestamp.strftime("%Y-%m-%d %H:%M"))
            action = escape_md(log.action)
            lines.append(f"• `{time_str}`: {action}")
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="MarkdownV2", reply_markup=menus.back_button()
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
                f"AI Safety Filter for **{escape_md(group.department or 'Group')}** is now {status_str}\\.",
                parse_mode="MarkdownV2",
                reply_markup=menus.back_button(),
            )
        else:
            await callback.message.edit_text("❌ Access Denied.", reply_markup=menus.back_button())
    await callback.answer()


@router.callback_query(F.data == "menu:cmd_version")
async def cb_menu_version(callback: types.CallbackQuery) -> None:
    text = (
        "🚀 *Academic Assistant Bot v1\\.1\\.0*\n\n"
        "*Recent Updates:*\n"
        "• OCR Image Extraction\n"
        "• Document Extraction \\(PDF/DOCX\\)\n"
        "• Voice Note Transcription\n"
        "• Duplicate Semantic Detection\n"
        "• Advanced Role Management & Audit Logs"
    )
    await callback.message.edit_text(
        text, parse_mode="MarkdownV2", reply_markup=menus.back_button()
    )
    await callback.answer()
