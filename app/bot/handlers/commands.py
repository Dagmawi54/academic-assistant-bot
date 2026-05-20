"""Command handlers: /start, /help, /menu, /status, /ask."""

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
        "`/status` — Show group info\n"
        "`/ask` — Ask the AI a question\n"
        "`/sync_admin` — Sync your admin role \\(in group\\)"
    )
    await message.answer(text)


@router.message(Command("menu"), F.chat.type == "private")
async def cmd_menu(message: types.Message, session: AsyncSession) -> None:
    """Show admin menu (DM only)."""
    is_admin = await is_admin_in_any_group(session, message.from_user.id)

    if is_admin:
        managed = await crud.get_managed_groups(session, message.from_user.id)
        group_names = ", ".join([escape_md(g.department) for g in managed if g.department]) or "None"
        await message.answer(
            f"⚙️ *Admin Menu*\n_Managing groups:_ {group_names}",
            reply_markup=menus.main_menu(),
        )
    else:
        text = (
            "⚠️ You don't have admin access to any registered active groups\\.\n\n"
            "If you are simply a student reading announcements, you don't need this menu\\!\n\n"
            "If you are a Telegram administrator in a registered group, go to that group and type `/sync_admin` first\\.\n\n"
            "If you just added me to a *new* group and want to become the Owner to manage it, click below to set it up:"
        )
        await message.answer(text, reply_markup=menus.unregistered_menu())


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


@router.message(Command("ask"))
async def cmd_ask(message: types.Message) -> None:
    """Ask the AI a technical or general question."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Please provide a question after the command, e.g. `/ask What is python?`", parse_mode="Markdown")
        return
        
    query = args[1].strip()

    from app.ai.groq_client import groq_client
    
    # Send temporary processing message
    status_msg = await message.answer("⏳ Thinking...")
    
    try:
        # Try Groq first for speed
        messages = [
            {"role": "system", "content": "You are a helpful academic and technical AI assistant. Keep responses reasonably concise."},
            {"role": "user", "content": query},
        ]
        result = await groq_client.complete(messages)

        answer_text = None
        if result:
            answer_text = result.get("raw") or None

        # If Groq failed or returned empty, try Gemini
        if not answer_text:
            try:
                from app.ai.gemini_client import gemini_client
                if gemini_client.is_configured:
                    gem_result = await gemini_client.complete(
                        "You are a helpful academic and technical AI assistant.", query
                    )
                    if gem_result:
                        answer_text = gem_result.get("raw") or None
            except Exception:
                pass

        if answer_text:
            # Truncate if too long for Telegram (4096 char limit)
            if len(answer_text) > 4000:
                answer_text = answer_text[:4000] + "\n\n... (truncated)"
            try:
                await status_msg.edit_text(answer_text, parse_mode="Markdown")
            except Exception:
                # If Markdown parsing fails, send as plain text
                await status_msg.edit_text(answer_text)
        else:
            await status_msg.edit_text("❌ Sorry, I couldn't reach the AI services right now. Check that API keys are configured.")
    except Exception as e:
        try:
            await status_msg.edit_text(f"❌ Error: {str(e)[:200]}")
        except Exception:
            pass
