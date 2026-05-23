"""Command handlers: /start, /help, /menu, /status, /ask.

All commands use StateFilter("*") so they work even when
the user is in the middle of an FSM wizard (e.g. Add Course).
"""

from aiogram import Router, types, F, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import any_state
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import menus
from app.admin.permissions import is_admin_in_any_group
from app.database import crud
import html
router = Router(name="commands")


# ---------- /start ----------
@router.message(Command("start"), StateFilter(any_state), F.chat.type == "private")
async def cmd_start(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Welcome message + admin menu if applicable."""
    await state.clear()
    user_id = message.from_user.id
    name = escape_md(message.from_user.first_name or "there")

    is_admin = await is_admin_in_any_group(session, user_id)

    if is_admin:
        text = (
            f"👋 Welcome, *{name}*\!\n\n"
            "You have admin access\. Use the menu below to manage your groups\."
        )
        await message.answer(text, reply_markup=menus.main_menu())
    else:
        text = (
            f"👋 Hi, *{name}*\!\n\n"
            "I'm the Academic Assistant Bot\. "
            "I help manage course information, assignments, and reminders "
            "in your university group\.\n\n"
            "If you're a group admin, add me to your group and use /menu here to configure\."
        )
        await message.answer(text)


# ---------- /help ----------
@router.message(Command("help"), StateFilter(any_state))
async def cmd_help(message: types.Message, state: FSMContext) -> None:
    """Show available commands."""
    await state.clear()
    text = (
        "<b>Available Commands</b>\n\n"
        "<code>/start</code> — Start the bot\n"
        "<code>/help</code> — Show this message\n"
        "<code>/menu</code> — Open admin menu (DM only)\n"
        "<code>/status</code> — Show group info\n"
        "<code>/ask</code> — Ask the AI a question\n"
        "<code>/sync_admin</code> — Sync your admin role (in group)"
    )
    await message.answer(text)


# ---------- /menu ----------
@router.message(Command("menu"), StateFilter(any_state), F.chat.type == "private")
async def cmd_menu(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Show admin menu (DM only)."""
    await state.clear()
    is_admin = await is_admin_in_any_group(session, message.from_user.id)

    if is_admin:
        managed = await crud.get_managed_groups(session, message.from_user.id)
        group_names = ", ".join([html.escape(g.department) for g in managed if g.department]) or "None"
        await message.answer(
            f"⚙️ <b>Admin Menu</b>\n_Managing groups:_ {group_names}",
            reply_markup=menus.main_menu(),
        )
    else:
        text = (
            "⚠️ You don't have admin access to any registered active groups.\n\n"
            "If you are simply a student reading announcements, you don't need this menu!\n\n"
            "If you are a Telegram administrator in a registered group, go to that group and type <code>/sync_admin</code> first.\n\n"
            "If you just added me to a <b>new</b> group and want to become the Owner to manage it, click below to set it up:"
        )
        await message.answer(text, reply_markup=menus.unregistered_menu())


# ---------- /status ----------
@router.message(Command("status"), StateFilter(any_state))
async def cmd_status(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Show current group/semester status."""
    await state.clear()
    chat_id = message.chat.id
    group = await crud.get_group_by_chat_id(session, chat_id)

    if not group:
        await message.answer("❓ This group is not registered. Ask an admin to set it up.")
        return

    courses = await crud.get_active_courses(session, group.id)
    course_list = ", ".join(html.escape(c.course_name) for c in courses) or "None"

    text = (
        f"📊 <b>Group Status</b>\n\n"
        f"<code>Department</code> {html.escape(group.department or 'Not set')}\n"
        f"<code>Year</code> {group.year or 'Not set'}\n"
        f"<code>Section</code> {html.escape(group.section or 'Not set')}\n"
        f"<code>Semester</code> {group.semester or 'Not set'}\n"
        f"<code>Courses</code> {course_list}\n"
        f"<code>Active</code> {'Yes' if group.active else 'No'}"
    )
    await message.answer(text)


# ---------- Callback: back to main menu ----------
@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: types.CallbackQuery) -> None:
    """Return to main admin menu."""
    await callback.message.edit_text("⚙️ <b>Admin Menu</b>", reply_markup=menus.main_menu())
    await callback.answer()


# ---------- Callback: cancel any wizard ----------
@router.callback_query(F.data == "cancel", StateFilter(any_state))
async def cb_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Cancel any active FSM flow."""
    await state.clear()
    await callback.message.edit_text(
        "❌ Cancelled. Use /menu to start again.",
        reply_markup=menus.back_button(),
    )
    await callback.answer()


# ---------- /ask (text-only) ----------
@router.message(Command("ask"), StateFilter(any_state), F.document == None)
async def cmd_ask(message: types.Message, state: FSMContext) -> None:
    """Ask the AI a text-only question."""
    await state.clear()
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Please provide a question after the command.\n"
            "Examples:\n"
            "  /ask What is python?\n"
            "  Or attach a DOCX/PDF file with /ask as the caption!",
            parse_mode=None,
        )
        return

    query = args[1].strip()
    await _process_ask(message, query, file_context=None)


# ---------- /ask (with document) ----------
@router.message(Command("ask"), StateFilter(any_state), F.document != None)
async def cmd_ask_with_file(message: types.Message, state: FSMContext, bot: Bot) -> None:
    """Ask the AI a question about an attached document."""
    await state.clear()
    
    # Extract question from caption
    caption = message.caption or ""
    args = caption.split(maxsplit=1)
    query = args[1].strip() if len(args) >= 2 else "Summarize and analyze this document."

    doc = message.document
    file_name = doc.file_name or "unknown"
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

    if ext not in ("pdf", "docx", "doc", "txt"):
        await message.answer(
            f"I can analyze PDF, DOCX, and TXT files. Got: .{ext}",
            parse_mode=None,
        )
        return

    # Size guard (5MB max)
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await message.answer("File too large (max 5MB).", parse_mode=None)
        return

    status_msg = await message.answer(f"📄 Reading {file_name}...", parse_mode=None)

    try:
        file = await bot.download(doc)
        file_bytes = file.read()

        from app.files.parser import extract_text_from_pdf, extract_text_from_docx

        extracted = None
        if ext == "pdf":
            extracted = await extract_text_from_pdf(file_bytes)
        elif ext in ("docx", "doc"):
            extracted = await extract_text_from_docx(file_bytes)
        elif ext == "txt":
            extracted = file_bytes.decode("utf-8", errors="replace")

        if not extracted or not extracted.strip():
            await status_msg.edit_text(
                "Could not extract any text from this file. It might be image-based or empty.",
                parse_mode=None,
            )
            return

        # Truncate extracted text to keep within token limits
        if len(extracted) > 8000:
            extracted = extracted[:8000] + "\n\n... (document truncated)"

        await status_msg.edit_text("⏳ Analyzing document...", parse_mode=None)
        file_context = f"--- DOCUMENT: {file_name} ---\n{extracted}\n--- END DOCUMENT ---"
        await _process_ask(message, query, file_context=file_context, status_msg=status_msg)

    except Exception as e:
        try:
            await status_msg.edit_text(f"Error reading file: {str(e)[:200]}", parse_mode=None)
        except Exception:
            pass


async def _process_ask(
    message: types.Message,
    query: str,
    file_context: str | None = None,
    status_msg: types.Message | None = None,
) -> None:
    """Shared AI query logic for text-only and file-attached /ask."""
    from app.ai.groq_client import groq_client

    if not status_msg:
        status_msg = await message.answer("⏳ Thinking...", parse_mode=None)

    user_content = query
    if file_context:
        user_content = f"{file_context}\n\nUser question: {query}"

    if getattr(message, "reply_to_message", None) and message.reply_to_message.text:
        role = "the assistant" if message.reply_to_message.from_user.id == message.bot.id else "another user"
        reply_context = f"[Replying to a previous message from {role}]:\n{message.reply_to_message.text}\n\n[My new question]:\n"
        user_content = reply_context + user_content

    try:
        sys_prompt = (
            "You are a highly intelligent, sophisticated academic and technical assistant built specifically "
            "for Academic Group Management. "
            "IMPORTANT TELEGRAM HTML FORMATTING RULES:\n"
            "1. NEVER use Markdown (**, *, #). It will look broken. Only use HTML: <b>bold</b>, <i>italic</i>, <code>code</code>.\n"
            "2. NEVER use asterisks (*) or hyphens (-) for lists. Use real bullets (•) or emojis (📌, ◾️, ✅).\n"
            "3. Structure your response with visually distinct sections. Use <b>SECTION TITLE</b> for headers, and leave a blank line before and after.\n"
            "4. Use <blockquote>text</blockquote> heavily whenever you are stating an important rule, a direct quote, or a core summary.\n"
            "5. Keep the text highly scannable, beautifully spaced, and avoid dense walls of text."
        )
        if file_context:
            sys_prompt += " If a document is provided, thoroughly analyze its contents and draw heavily from it."

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
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
                    gem_result = await gemini_client.complete(sys_prompt, user_content)
                    if gem_result:
                        answer_text = gem_result.get("raw") or None
            except Exception:
                pass

        if answer_text:
            if len(answer_text) > 4000:
                answer_text = answer_text[:4000] + "\n\n... (truncated)"
            
            from app.utils.text import sanitize_telegram_html
            safe_html = sanitize_telegram_html(answer_text)
            
            try:
                await status_msg.edit_text(safe_html, parse_mode="HTML")
            except Exception:
                try:
                    await status_msg.edit_text(answer_text, parse_mode=None)
                except Exception:
                    await status_msg.edit_text("Got a response but couldn't display it.", parse_mode=None)
        else:
            await status_msg.edit_text(
                "Sorry, I couldn't reach the AI services right now. "
                "Check that GROQ_API_KEY or GEMINI_API_KEY env vars are configured on Render.",
                parse_mode=None,
            )
    except Exception as e:
        try:
            await status_msg.edit_text(f"Error: {str(e)[:200]}", parse_mode=None)
        except Exception:
            pass

