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


@router.message(Command("ask"), StateFilter(any_state), F.document == None)
async def cmd_ask(message: types.Message, state: FSMContext, bot: Bot) -> None:
    """Ask the AI a text-only question."""
    await state.clear()
    args = message.text.split(maxsplit=1)
    reply_context = await _extract_reply_media_context(message, bot)
    if len(args) < 2 and not reply_context:
        await message.answer(
            "Please provide a question after the command.\n"
            "Examples:\n"
            "  /ask What is python?\n"
            "  Or reply /ask to an image, voice note, PDF, DOCX, or TXT file.",
            parse_mode=None,
        )
        return

    query = args[1].strip() if len(args) >= 2 else "Analyze this replied content and explain the useful academic points."
    await _process_ask(message, query, file_context=reply_context)


@router.message(Command("image_scan"), StateFilter(any_state), F.photo != None)
@router.message(Command("ask"), StateFilter(any_state), F.photo != None)
async def cmd_image_scan(message: types.Message, state: FSMContext, bot: Bot) -> None:
    """Analyze attached photos via OCR and ask the AI."""
    await state.clear()
    caption = message.caption or ""
    args = caption.split(maxsplit=1)
    query = args[1].strip() if len(args) >= 2 else "Analyze this image and explain its academic context."

    if not message.photo:
        await message.answer("Please attach an image.", parse_mode=None)
        return

    status_msg = await message.answer("🖼 Reading image...", parse_mode=None)
    try:
        photo = message.photo[-1]
        downloaded = await bot.download(photo)
        image_bytes = downloaded.read()
        
        from app.ocr.engine import extract_text_from_image
        extracted = await extract_text_from_image(image_bytes)

        if not extracted or not extracted.strip():
            await status_msg.edit_text("Could not extract any text from this image.", parse_mode=None)
            return

        if len(extracted) > 8000:
            extracted = extracted[:8000] + "\n\n... (content truncated)"
            
        await status_msg.edit_text("⏳ Analyzing image...", parse_mode=None)
        file_context = f"--- IMAGE OCR ---\n{extracted}\n--- END IMAGE OCR ---"
        await _process_ask(message, query, file_context=file_context, status_msg=status_msg)
    except Exception as e:
        try:
            await status_msg.edit_text(f"Error reading image: {str(e)[:200]}", parse_mode=None)
        except Exception:
            pass


@router.message(Command("ask"), StateFilter(any_state), F.document != None)
async def cmd_ask_with_file(message: types.Message, state: FSMContext, bot: Bot) -> None:
    """Ask the AI a question about an attached document."""
    await state.clear()

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

    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await message.answer("File too large (max 5MB).", parse_mode=None)
        return

    status_msg = await message.answer(f"📄 Reading {file_name}...", parse_mode=None)

    try:
        file = await bot.download(doc)
        file_bytes = file.read()

        extracted = await _extract_document_text(file_bytes, ext)

        if not extracted or not extracted.strip():
            await status_msg.edit_text(
                "Could not extract any text from this file. It might be image-based or empty.",
                parse_mode=None,
            )
            return

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

    if getattr(message, "reply_to_message", None) and message.reply_to_message.text:
        role = "the assistant" if message.reply_to_message.from_user.id == message.bot.id else "another user"
        reply_context = (
            f"[Replying to a previous message from {role}]:\n{message.reply_to_message.text}\n\n"
            "[My new question]:\n"
        )
        user_content = reply_context + user_content

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
        sys_prompt += "\nIf a document is provided, thoroughly analyze its contents and draw heavily from it."

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


async def _extract_reply_media_context(message: types.Message, bot: Bot) -> str | None:
    """Extract text from replied media so /ask can analyze it."""
    reply = getattr(message, "reply_to_message", None)
    if not reply:
        return None

    if reply.text:
        return f"--- REPLIED MESSAGE ---\n{reply.text}\n--- END REPLIED MESSAGE ---"

    try:
        if reply.photo:
            status = await message.answer("Reading the replied image...", parse_mode=None)
            photo = reply.photo[-1]
            downloaded = await bot.download(photo)
            image_bytes = downloaded.read()
            from app.ocr.engine import extract_text_from_image

            extracted = await extract_text_from_image(image_bytes)
            await _safe_delete(status)
            if extracted and extracted.strip():
                return _trim_context("REPLIED IMAGE OCR", extracted)
            await message.answer("I could not read text from that image.", parse_mode=None)
            return None

        if reply.document:
            doc = reply.document
            file_name = doc.file_name or "replied-file"
            ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
            if ext not in ("pdf", "docx", "doc", "txt"):
                await message.answer(f"I can analyze replied PDF, DOCX, and TXT files. Got: .{ext}", parse_mode=None)
                return None
            status = await message.answer(f"Reading {file_name}...", parse_mode=None)
            downloaded = await bot.download(doc)
            extracted = await _extract_document_text(downloaded.read(), ext)
            await _safe_delete(status)
            if extracted and extracted.strip():
                return _trim_context(f"REPLIED DOCUMENT: {file_name}", extracted)
            await message.answer("I could not extract text from that replied file.", parse_mode=None)
            return None

        audio = reply.voice or reply.audio
        if audio:
            status = await message.answer("Transcribing the replied voice note...", parse_mode=None)
            downloaded = await bot.download(audio)
            audio_bytes = downloaded.read()
            from app.ai.groq_client import groq_client

            ext = "ogg" if reply.voice else "mp3"
            transcript = await groq_client.transcribe_audio(audio_bytes, f"reply.{ext}")
            await _safe_delete(status)
            if transcript and transcript.strip():
                return _trim_context("REPLIED VOICE TRANSCRIPT", transcript)
            await message.answer("I could not transcribe that voice note.", parse_mode=None)
            return None

    except Exception as exc:
        await message.answer(f"Could not analyze the replied media: {str(exc)[:160]}", parse_mode=None)
        return None

    return None


async def _extract_document_text(file_bytes: bytes, ext: str) -> str | None:
    from app.files.parser import extract_text_from_docx, extract_text_from_pdf

    if ext == "pdf":
        return await extract_text_from_pdf(file_bytes)
    if ext in ("docx", "doc"):
        return await extract_text_from_docx(file_bytes)
    if ext == "txt":
        return file_bytes.decode("utf-8", errors="replace")
    return None


def _trim_context(label: str, text: str) -> str:
    if len(text) > 8000:
        text = text[:8000] + "\n\n... (content truncated)"
    return f"--- {label} ---\n{text}\n--- END {label} ---"


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
