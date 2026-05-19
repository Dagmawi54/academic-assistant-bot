"""Command handlers: /start, /help, /menu, /status."""

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import menus
from app.admin.permissions import is_admin_in_any_group
from app.database import crud
from app.utils.text import escape_md

router = Router(name="commands")


@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: types.Message, session: AsyncSession) -> None:
    """Welcome message + admin menu if applicable."""
    user_id = message.from_user.id
    name = escape_md(message.from_user.first_name or "there")

    is_admin = await is_admin_in_any_group(session, user_id)

    if is_admin:
        text = (
            f"👋 Welcome, *{name}*\\!\n\n"
            "You have admin access\\. Use the menu below to manage your groups\\."
        )
        await message.answer(text, reply_markup=menus.main_menu())
    else:
        text = (
            f"👋 Hi, *{name}*\\!\n\n"
            "I'm the Academic Assistant Bot\\. "
            "I help manage course information, assignments, and reminders "
            "in your university group\\.\n\n"
            "If you're a group admin, add me to your group and use /menu here to configure\\."
        )
        await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    """Show available commands."""
    text = (
        "*Available Commands*\n\n"
        "`/start` — Start the bot\n"
        "`/help` — Show this message\n"
        "`/menu` — Open admin menu \\(DM only\\)\n"
        "`/status` — Show group info"
    )
    await message.answer(text)


@router.message(Command("menu"), F.chat.type == "private")
async def cmd_menu(message: types.Message, session: AsyncSession) -> None:
    """Show admin menu (DM only)."""
    is_admin = await is_admin_in_any_group(session, message.from_user.id)

    if is_admin:
        await message.answer("⚙️ *Admin Menu*", reply_markup=menus.main_menu())
    else:
        await message.answer(
            "⚠️ You don't have admin access to any registered group\\.\n\n"
            "Ask your group owner to assign you a role\\."
        )


@router.message(Command("status"))
async def cmd_status(message: types.Message, session: AsyncSession) -> None:
    """Show current group/semester status."""
    chat_id = message.chat.id
    group = await crud.get_group_by_chat_id(session, chat_id)

    if not group:
        await message.answer("❓ This group is not registered\\. Ask an admin to set it up\\.")
        return

    courses = await crud.get_active_courses(session, group.id)
    course_list = ", ".join(escape_md(c.course_name) for c in courses) or "None"

    text = (
        f"📊 *Group Status*\n\n"
        f"`Department` {escape_md(group.department or 'Not set')}\n"
        f"`Year` {group.year or 'Not set'}\n"
        f"`Section` {escape_md(group.section or 'Not set')}\n"
        f"`Semester` {group.semester or 'Not set'}\n"
        f"`Courses` {course_list}\n"
        f"`Active` {'Yes' if group.active else 'No'}"
    )
    await message.answer(text)


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: types.CallbackQuery) -> None:
    """Return to main admin menu."""
    await callback.message.edit_text("⚙️ *Admin Menu*", reply_markup=menus.main_menu())
    await callback.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Cancel any active FSM flow."""
    await state.clear()
    await callback.message.edit_text(
        "❌ Cancelled\\. Use /menu to start again\\.",
        reply_markup=menus.back_button(),
    )
    await callback.answer()


# /version removed per user request (moved to admin menu buttons)
