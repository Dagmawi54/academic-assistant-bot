"""Intent router — classifies user input type and extracts content without calling AI."""

from dataclasses import dataclass, field


@dataclass
class IntentResult:
    """Result of input intent detection."""

    type: str  # text | image | document | audio | unknown
    payload: str | None = None  # extracted content (OCR, parsed text, transcript)
    query: str = ""  # user's question text
    metadata: dict = field(default_factory=dict)


async def detect_intent(message, bot) -> IntentResult:
    """Detect the intent of a Telegram message and extract content.

    Rules:
        - Only extract + classify input type
        - NEVER call AI
        - NEVER apply business logic
    """
    from app.logging import get_logger

    logger = get_logger("intent_router")

    caption = message.caption or ""
    text = message.text or caption or ""

    # Strip command prefix from query
    query = text
    for prefix in ("/ask ", "/image_scan "):
        if query.lower().startswith(prefix):
            query = query[len(prefix):].strip()
            break
    # Handle bare commands
    if query.lower() in ("/ask", "/image_scan"):
        query = ""

    # --- Image (OCR) ---
    if message.photo:
        logger.info("intent_detected", intent="image", message_id=message.message_id)
        try:
            photo = message.photo[-1]
            downloaded = await bot.download(photo)
            image_bytes = downloaded.read()

            from app.ocr.engine import extract_text_from_image

            extracted = await extract_text_from_image(image_bytes)
            payload = extracted.strip() if extracted else None
            if payload and len(payload) > 8000:
                payload = payload[:8000] + "\n\n... (content truncated)"
            return IntentResult(
                type="image",
                payload=payload,
                query=query or "Analyze this image and explain its academic context.",
                metadata={"file_size": len(image_bytes)},
            )
        except Exception as exc:
            logger.exception("intent_image_extraction_failed", error=str(exc)[:200])
            return IntentResult(type="image", payload=None, query=query)

    # --- Document (PDF / DOCX / TXT) ---
    if message.document:
        doc = message.document
        file_name = doc.file_name or "unknown"
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        logger.info("intent_detected", intent="document", ext=ext, message_id=message.message_id)

        if ext not in ("pdf", "docx", "doc", "txt"):
            return IntentResult(
                type="document",
                payload=None,
                query=query,
                metadata={"error": f"Unsupported format: .{ext}", "file_name": file_name},
            )

        if doc.file_size and doc.file_size > 5 * 1024 * 1024:
            return IntentResult(
                type="document",
                payload=None,
                query=query,
                metadata={"error": "File too large (max 5MB)", "file_name": file_name},
            )

        try:
            downloaded = await bot.download(doc)
            file_bytes = downloaded.read()
            extracted = await _extract_document_text(file_bytes, ext)
            payload = extracted.strip() if extracted else None
            if payload and len(payload) > 8000:
                payload = payload[:8000] + "\n\n... (document truncated)"
            return IntentResult(
                type="document",
                payload=payload,
                query=query or "Summarize and analyze this document.",
                metadata={"file_name": file_name, "ext": ext},
            )
        except Exception as exc:
            logger.exception("intent_doc_extraction_failed", error=str(exc)[:200])
            return IntentResult(
                type="document",
                payload=None,
                query=query,
                metadata={"error": str(exc)[:200], "file_name": file_name},
            )

    # --- Audio / Voice ---
    audio_obj = message.voice or message.audio
    if audio_obj:
        logger.info("intent_detected", intent="audio", message_id=message.message_id)
        try:
            downloaded = await bot.download(audio_obj)
            audio_bytes = downloaded.read()
            ext = "ogg" if message.voice else "mp3"

            from app.ai.groq_client import groq_client

            transcript = await groq_client.transcribe_audio(audio_bytes, f"audio.{ext}")
            payload = transcript.strip() if transcript else None
            return IntentResult(
                type="audio",
                payload=payload,
                query=query or "Analyze this audio content.",
                metadata={"ext": ext},
            )
        except Exception as exc:
            logger.exception("intent_audio_extraction_failed", error=str(exc)[:200])
            return IntentResult(type="audio", payload=None, query=query)

    # --- Reply media (check replied message) ---
    reply = getattr(message, "reply_to_message", None)
    if reply:
        reply_intent = await _extract_reply_content(reply, bot, logger)
        if reply_intent:
            return IntentResult(
                type=reply_intent.type,
                payload=reply_intent.payload,
                query=query or "Analyze this replied content and explain the useful academic points.",
                metadata=reply_intent.metadata,
            )

    # --- Plain text ---
    logger.info("intent_detected", intent="text", message_id=message.message_id)
    return IntentResult(type="text", payload=None, query=query)


async def _extract_reply_content(reply, bot, logger) -> IntentResult | None:
    """Extract content from a replied message."""
    if reply.text:
        role = "the assistant" if reply.from_user and reply.from_user.is_bot else "another user"
        context = f"[Replying to a previous message from {role}]:\n{reply.text}"
        return IntentResult(type="text", payload=context, query="")

    if reply.photo:
        try:
            photo = reply.photo[-1]
            downloaded = await bot.download(photo)
            image_bytes = downloaded.read()
            from app.ocr.engine import extract_text_from_image

            extracted = await extract_text_from_image(image_bytes)
            if extracted and extracted.strip():
                return IntentResult(type="image", payload=extracted.strip(), query="")
        except Exception:
            logger.exception("reply_image_extraction_failed")

    if reply.document:
        doc = reply.document
        ext = doc.file_name.rsplit(".", 1)[-1].lower() if doc.file_name and "." in doc.file_name else ""
        if ext in ("pdf", "docx", "doc", "txt"):
            try:
                downloaded = await bot.download(doc)
                extracted = await _extract_document_text(downloaded.read(), ext)
                if extracted and extracted.strip():
                    return IntentResult(
                        type="document",
                        payload=extracted.strip(),
                        query="",
                        metadata={"file_name": doc.file_name or "replied-file"},
                    )
            except Exception:
                logger.exception("reply_doc_extraction_failed")

    audio = reply.voice or reply.audio
    if audio:
        try:
            downloaded = await bot.download(audio)
            audio_bytes = downloaded.read()
            ext = "ogg" if reply.voice else "mp3"
            from app.ai.groq_client import groq_client

            transcript = await groq_client.transcribe_audio(audio_bytes, f"reply.{ext}")
            if transcript and transcript.strip():
                return IntentResult(type="audio", payload=transcript.strip(), query="")
        except Exception:
            logger.exception("reply_audio_extraction_failed")

    return None


async def _extract_document_text(file_bytes: bytes, ext: str) -> str | None:
    """Extract text from a document by extension."""
    from app.files.parser import extract_text_from_docx, extract_text_from_pdf

    if ext == "pdf":
        return await extract_text_from_pdf(file_bytes)
    if ext in ("docx", "doc"):
        return await extract_text_from_docx(file_bytes)
    if ext == "txt":
        return file_bytes.decode("utf-8", errors="replace")
    return None
