"""Direct Message background chatbot handler."""

from aiogram import Router, types, F, Bot
from aiogram.filters import StateFilter

from app.bot.handlers.commands import _process_ask
from app.logging import get_logger

logger = get_logger("dm_handler")
router = Router(name="dm")


@router.message(StateFilter(None), F.chat.type == "private")
async def chat_with_bot(message: types.Message, bot: Bot) -> None:
    """Seamlessly chat with the bot in DMs without needing /ask.
    Only triggers if the user has no active FSM wizard (StateFilter(None)).
    Handles text, documents, voice, audio, and photos identically to /ask.
    """

    # --- Document uploads ---
    doc = message.document
    if doc:
        query = message.caption or "Please analyze and summarize this document."

        file_name = doc.file_name or "unknown"
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

        if ext not in ("pdf", "docx", "doc", "txt"):
            await message.answer(
                f"I can read PDF, DOCX, and TXT files. Got: .{ext}",
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
                    "Could not extract any text from this file.",
                    parse_mode=None,
                )
                return

            if len(extracted) > 8000:
                extracted = extracted[:8000] + "\n\n... (document truncated)"

            await status_msg.edit_text("⏳ Analyzing document...", parse_mode=None)
            file_context = f"--- DOCUMENT: {file_name} ---\n{extracted}\n--- END DOCUMENT ---"

            await _process_ask(message, query=query, file_context=file_context, status_msg=status_msg)

        except Exception as e:
            try:
                await status_msg.edit_text(f"Error reading file: {str(e)[:200]}", parse_mode=None)
            except Exception:
                pass
        return

    # --- Voice / Audio messages ---
    audio_obj = message.voice or message.audio
    if audio_obj:
        status_msg = await message.answer("🎙️ Listening...", parse_mode=None)
        try:
            from app.ai.groq_client import groq_client

            downloaded = await bot.download(audio_obj)
            audio_bytes = downloaded.read()
            ext = "ogg" if message.voice else "mp3"
            transcript = await groq_client.transcribe_audio(audio_bytes, f"audio.{ext}")

            if transcript and transcript.strip():
                file_context = f"[VOICE TRANSCRIPTION]\n{transcript.strip()}"
                query = message.caption or "Respond to this voice message."
                await status_msg.edit_text("⏳ Thinking...", parse_mode=None)
                await _process_ask(message, query, file_context=file_context, status_msg=status_msg)
            else:
                await status_msg.edit_text(
                    "Couldn't catch that — try again or type it out?", parse_mode=None
                )
        except Exception:
            logger.exception("dm_voice_processing_failed")
            try:
                await status_msg.edit_text(
                    "Voice processing failed. Try typing your message instead.", parse_mode=None
                )
            except Exception:
                pass
        return

    # --- Photo messages (OCR) ---
    if message.photo:
        status_msg = await message.answer("🔍 Analyzing image...", parse_mode=None)
        try:
            from app.ocr.engine import extract_text_from_image

            photo = message.photo[-1]
            downloaded = await bot.download(photo)
            image_bytes = downloaded.read()
            ocr_text = await extract_text_from_image(image_bytes)

            query = message.caption or "Analyze and explain this image."
            file_context = f"[IMAGE OCR DATA]\n{ocr_text}" if ocr_text else None

            await status_msg.edit_text("⏳ Thinking...", parse_mode=None)
            await _process_ask(message, query, file_context=file_context, status_msg=status_msg)
        except Exception:
            logger.exception("dm_photo_processing_failed")
            try:
                await status_msg.edit_text(
                    "Image processing failed. Try again or describe your question.", parse_mode=None
                )
            except Exception:
                pass
        return

    # --- Standard text message ---
    if message.text:
        await _process_ask(message, query=message.text, file_context=None)

