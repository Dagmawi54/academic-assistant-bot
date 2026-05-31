"""Command handlers: /start, /help, /menu, /status, /ask."""

import json
from collections import defaultdict, deque

from aiogram import Bot, F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import any_state
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import menus
from app.admin.permissions import is_admin_in_any_group
from app.database import crud
from app.utils.text import ResponseFormatter
from app.services.intent_router import detect_intent

import html

router = Router(name="commands")
_conversation_memory: dict[tuple[int, int | None, int], deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=6))


@router.message(Command("start"), StateFilter(any_state), F.chat.type == "private")
async def cmd_start(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Welcome message + admin menu if applicable."""
    await state.clear()
    user_id = message.from_user.id
    name = html.escape(message.from_user.first_name or "there")

    if await is_admin_in_any_group(session, user_id):
        await message.answer(
            f"👋 <b>Welcome, {name}</b>\n\nYou have admin access. Use the menu below to manage your groups.",
            reply_markup=menus.main_menu(),
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"👋 <b>Hi, {name}</b>\n\n"
        "I'm the Academic Assistant Bot. I help manage course information, assignments, and reminders in your university group.\n\n"
        "If you're a group admin, add me to your group and use /menu here to configure.",
        parse_mode="HTML",
    )


@router.message(Command("help"), StateFilter(any_state))
async def cmd_help(message: types.Message, state: FSMContext) -> None:
    """Show available commands."""
    await state.clear()
    text = (
        "<b>Available Commands</b>\n\n"
        "<code>/start</code> - Start the bot\n"
        "<code>/help</code> - Show this message\n"
        "<code>/menu</code> - Open admin menu (DM only)\n"
        "<code>/status</code> - Show group info\n"
        "<code>/ask</code> - Ask the AI a question\n"
        "<code>/sync_admin</code> - Sync your admin role (in group)"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("menu"), StateFilter(any_state), F.chat.type == "private")
async def cmd_menu(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Show admin menu (DM only)."""
    await state.clear()
    is_admin = await is_admin_in_any_group(session, message.from_user.id)

    if is_admin:
        managed = await crud.get_managed_groups(session, message.from_user.id)
        group_names = ", ".join(html.escape(g.department) for g in managed if g.department) or "None"
        await message.answer(
            f"⚙️ <b>Admin Menu</b>\n<i>Managing groups:</i> {group_names}",
            reply_markup=menus.main_menu(),
            parse_mode="HTML",
        )
        return

    text = (
        "⚠️ You don't have admin access to any registered active groups.\n\n"
        "If you are simply a student reading announcements, you don't need this menu.\n\n"
        "If you are a Telegram administrator in a registered group, go to that group and type <code>/sync_admin</code> first.\n\n"
        "If you just added me to a <b>new</b> group and want to become the owner to manage it, click below to set it up:"
    )
    await message.answer(text, reply_markup=menus.unregistered_menu(), parse_mode="HTML")


@router.message(Command("status"), StateFilter(any_state))
async def cmd_status(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Show current group/semester status."""
    await state.clear()
    group = await crud.get_group_by_chat_id(session, message.chat.id)

    if not group:
        await message.answer("❓ This group is not registered. Ask an admin to set it up.", parse_mode="HTML")
        return

    courses = await crud.get_active_courses(session, group.id)
    course_list = ", ".join(html.escape(c.course_name) for c in courses) or "None"
    text = (
        "📊 <b>Group Status</b>\n\n"
        f"<code>Department</code> {html.escape(group.department or 'Not set')}\n"
        f"<code>Year</code> {group.year or 'Not set'}\n"
        f"<code>Section</code> {html.escape(group.section or 'Not set')}\n"
        f"<code>Semester</code> {group.semester or 'Not set'}\n"
        f"<code>Courses</code> {course_list}\n"
        f"<code>Active</code> {'Yes' if group.active else 'No'}"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("debug_runtime"), StateFilter(any_state), F.chat.type == "private")
async def cmd_debug_runtime(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Show live runtime diagnostics for admins."""
    await state.clear()
    if not await is_admin_in_any_group(session, message.from_user.id):
        await message.answer("You do not have admin access to runtime diagnostics.", parse_mode=None)
        return

    from app.bot import bot, storage
    from app.services.runtime_diagnostics import collect_runtime_diagnostics, render_runtime_diagnostics

    report = await collect_runtime_diagnostics(bot=bot, session=session, fsm_storage=storage)
    text = render_runtime_diagnostics(report)
    if len(text) > 3900:
        text = text[:3900] + "\n\n<code>... truncated</code>"
    await message.answer(text, parse_mode="HTML")


@router.message(Command("acceptance"), StateFilter(any_state), F.chat.type == "private")
async def cmd_acceptance(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Show manual runtime acceptance records for admins."""
    await state.clear()
    if not await is_admin_in_any_group(session, message.from_user.id):
        await message.answer("You do not have admin access to runtime acceptance.", parse_mode=None)
        return

    from app.services.acceptance_service import acceptance_dashboard_markup, render_acceptance_dashboard

    text = await render_acceptance_dashboard(session)
    await message.answer(
        text[:3900],
        reply_markup=acceptance_dashboard_markup(),
        parse_mode="HTML",
    )


@router.message(Command("ask_diagnostics"), StateFilter(any_state), F.chat.type == "private")
async def cmd_ask_diagnostics(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Show /ask provider and media-support diagnostics."""
    await state.clear()
    if not await is_admin_in_any_group(session, message.from_user.id):
        await message.answer("You do not have admin access to /ask diagnostics.", parse_mode=None)
        return

    from app.services.acceptance_service import collect_ask_diagnostics, render_ask_diagnostics

    report = await collect_ask_diagnostics(session)
    await crud.log_action(
        session,
        action="ask_provider_check",
        telegram_user_id=message.from_user.id,
        details=json.dumps(report, default=str, sort_keys=True),
    )
    await message.answer(render_ask_diagnostics(report), parse_mode="HTML")


@router.message(Command("event_diagnostics"), StateFilter(any_state), F.chat.type == "private")
async def cmd_event_diagnostics(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Show Academic OS event pipeline diagnostics."""
    await state.clear()
    if not await is_admin_in_any_group(session, message.from_user.id):
        await message.answer("You do not have admin access to event diagnostics.", parse_mode=None)
        return

    from app.services.acceptance_service import collect_event_diagnostics, render_event_diagnostics

    report = await collect_event_diagnostics(session)
    await crud.log_action(
        session,
        action="scheduler_snapshot",
        telegram_user_id=message.from_user.id,
        details=json.dumps(report, default=str, sort_keys=True),
    )
    await message.answer(render_event_diagnostics(report), parse_mode="HTML")


@router.callback_query(F.data == "accept:refresh")
async def cb_acceptance_refresh(callback: types.CallbackQuery, session: AsyncSession) -> None:
    from app.services.acceptance_service import acceptance_dashboard_markup, render_acceptance_dashboard

    await callback.message.edit_text(
        (await render_acceptance_dashboard(session))[:3900],
        reply_markup=acceptance_dashboard_markup(),
        parse_mode="HTML",
    )
    await callback.answer("Acceptance refreshed.")


@router.callback_query(F.data.startswith("accept:view:"))
async def cb_acceptance_feature(callback: types.CallbackQuery) -> None:
    from app.services.acceptance_service import (
        FEATURE_BY_SLUG,
        acceptance_feature_markup,
        render_acceptance_feature,
    )

    slug = callback.data.rsplit(":", 1)[1]
    feature = FEATURE_BY_SLUG.get(slug)
    if not feature:
        await callback.answer("Unknown feature.", show_alert=True)
        return
    await callback.message.edit_text(
        render_acceptance_feature(feature),
        reply_markup=acceptance_feature_markup(feature),
        parse_mode="HTML",
    )
    await callback.answer("Feature opened.")


@router.callback_query(F.data.startswith("accept:pass:") | F.data.startswith("accept:fail:"))
async def cb_acceptance_mark(callback: types.CallbackQuery, session: AsyncSession) -> None:
    from app.services.acceptance_service import (
        FEATURE_BY_SLUG,
        acceptance_dashboard_markup,
        record_acceptance_result,
        render_acceptance_dashboard,
    )

    _, action, slug = callback.data.split(":", 2)
    feature = FEATURE_BY_SLUG.get(slug)
    if not feature:
        await callback.answer("Unknown feature.", show_alert=True)
        return
    result = "passed" if action == "pass" else "failed"
    await record_acceptance_result(
        session,
        feature_name=feature,
        result=result,
        tester=callback.from_user.id,
        reason="Manual Telegram runtime acceptance",
    )
    await callback.message.edit_text(
        (await render_acceptance_dashboard(session))[:3900],
        reply_markup=acceptance_dashboard_markup(),
        parse_mode="HTML",
    )
    await callback.answer(f"{feature}: {result}.")


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: types.CallbackQuery) -> None:
    """Return to main admin menu."""
    await callback.message.edit_text("⚙️ <b>Admin Menu</b>", reply_markup=menus.main_menu(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "cancel", StateFilter(any_state))
async def cb_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Cancel any active FSM flow."""
    await state.clear()
    await callback.message.edit_text(
        "❌ Cancelled. Use /menu to start again.",
        reply_markup=menus.back_button(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(Command("ask"), StateFilter(any_state))
@router.message(Command("image_scan"), StateFilter(any_state))
async def cmd_ask_unified(message: types.Message, state: FSMContext, bot: Bot) -> None:
    """Unified handler for all /ask and AI proxy inputs."""
    from app.logging import get_logger
    logger = get_logger("cmd_ask_unified")
    await state.clear()
    
    intent = await detect_intent(message, bot)
    trace_id = getattr(message, "_trace_id", None) or "no-trace"
    logger.info("ASK_ENTRY", trace_id=trace_id, intent_type=intent.type, has_payload=bool(intent.payload), user_id=message.from_user.id if message.from_user else 0)

    if intent.metadata.get("error"):
        await message.answer(f"Error: {intent.metadata['error']}", parse_mode=None)
        return

    content = intent.query
    file_context = None

    if intent.payload:
        content = intent.query or "Analyze the attached content and explain the useful academic points."
        content_type = intent.type.upper()
        file_context = f"--- {content_type} CONTENT ---\n{intent.payload}\n--- END {content_type} CONTENT ---"

    if not content.strip() and not file_context:
        await message.answer(
            "Please provide a question after the command.\n"
            "Examples:\n"
            "  /ask What is python?\n"
            "  Or reply /ask to an image, voice note, PDF, DOCX, or TXT file.",
            parse_mode=None,
        )
        return

    status_msg = await message.answer("⏳ Processing input...", parse_mode=None)
    await _process_ask(message, content, file_context=file_context, status_msg=status_msg)

# ================= LEGACY ALIAS LAYER =================
# Safe transition stubs, do not delete yet.

async def cmd_ask(message: types.Message, state: FSMContext, bot: Bot) -> None:
    return await cmd_ask_unified(message, state, bot)

async def cmd_image_scan(message: types.Message, state: FSMContext, bot: Bot) -> None:
    return await cmd_ask_unified(message, state, bot)

async def cmd_ask_with_file(message: types.Message, state: FSMContext, bot: Bot) -> None:
    return await cmd_ask_unified(message, state, bot)
# ======================================================


@router.message(StateFilter(any_state), F.text, F.reply_to_message)
async def continue_bot_conversation(message: types.Message, state: FSMContext, bot: Bot) -> None:
    """Continue a short conversation when a user replies to the bot."""
    reply = message.reply_to_message
    if not reply or not reply.from_user or reply.from_user.id != bot.id:
        return
    await state.clear()
    await _process_ask(message, message.text.strip(), file_context=None)


async def _process_ask(
    message: types.Message,
    query: str,
    file_context: str | None = None,
    status_msg: types.Message | None = None,
) -> None:
    """Shared AI query logic for text-only and file-attached /ask."""
    from app.ai.chatbot_client import chatbot_client

    if not status_msg:
        status_msg = await message.answer("⏳ Thinking...", parse_mode=None)

    user_content = query
    if file_context:
        user_content = f"{file_context}\n\nUser question: {query}"

    memory_key = _memory_key(message)
    prior_turns = list(_conversation_memory[memory_key])

    # Fix 3: Dynamic personality Injection
    base_system_prompt = (
        "Respond with structured clarity.\n"
        "Use sections and bullet points when needed.\n"
        "Do not produce unstructured long paragraphs.\n"
        "Do not reduce necessary detail.\n"
        "Avoid repetition and unnecessary verbosity.\n"
        "IMPORTANT RULES:\n"
        "1. NEVER use Markdown. Only use HTML: <b>bold</b>, <i>italic</i>, <code>code</code>.\n"
        "2. For multi-line code, use <pre><code class=\"language-python\">...</code></pre> when the language is clear.\n"
        "3. Do not expose raw HTML tags to the user.\n"
        "4. STRICTLY PROHIBITED: giant walls of text, excessive emojis, and generic AI introductions."
    )
    
    personality_profile = getattr(message.bot, "personality_profile", "You are a helpful, professional academic assistant.")
    
    sys_prompt = f"{personality_profile}\n\n{base_system_prompt}"
    
    if file_context:
        if "IMAGE CONTENT" in file_context:
            sys_prompt += "\nAn image was provided. Note: The OCR text might contain inaccuracies (especially Amharic). CRITICAL: If the OCR text is mostly unreadable symbols or gibberish, explicitly state that you cannot read the image. DO NOT GUESS OR INVENT details."
        sys_prompt += "\nIf a document/media is provided, thoroughly analyze its contents and draw heavily from it."

    try:
        messages = [{"role": "system", "content": sys_prompt}]
        messages.extend(prior_turns)
        messages.append({"role": "user", "content": user_content})
        result = await chatbot_client.complete(messages)
        answer_text = result.get("raw") if result else None

        if answer_text:
            if len(answer_text) > 4000:
                answer_text = answer_text[:4000] + "\n\n... (truncated)"

            safe_html = ResponseFormatter.normalize(answer_text)
            try:
                await status_msg.edit_text(safe_html, parse_mode="HTML")
                _remember_turn(memory_key, user_content, safe_html)
            except Exception:
                try:
                    await status_msg.edit_text(answer_text, parse_mode=None)
                    _remember_turn(memory_key, user_content, answer_text)
                except Exception:
                    await status_msg.edit_text("Got a response but couldn't display it.", parse_mode=None)
        else:
            await status_msg.edit_text(
                "Sorry, I couldn't reach the AI services right now. Check that GROQ_API_KEY or GEMINI_API_KEY env vars are configured on Render.",
                parse_mode=None,
            )
    except Exception as e:
        try:
            await status_msg.edit_text(f"Error: {str(e)[:200]}", parse_mode=None)
        except Exception:
            pass





def _memory_key(message: types.Message) -> tuple[int, int | None, int]:
    return (
        message.chat.id,
        getattr(message, "message_thread_id", None),
        message.from_user.id if message.from_user else 0,
    )


def _remember_turn(key: tuple[int, int | None, int], user_content: str, answer_text: str) -> None:
    _conversation_memory[key].append({"role": "user", "content": user_content[-2500:]})
    _conversation_memory[key].append({"role": "assistant", "content": answer_text[-2500:]})


async def _safe_delete(message: types.Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass
