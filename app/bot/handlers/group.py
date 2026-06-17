"""Group message handler — thin intake that delegates to service layer."""

from aiogram import Router, types, F
from aiogram.enums import ChatType
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.services.routing_service import process_group_message

logger = get_logger("group_handler")

router = Router(name="group")


@router.message(F.forum_topic_created)
async def handle_topic_created(message: types.Message, session: AsyncSession) -> None:
    """Register newly created forum topics in the database."""
    from app.database import crud
    from app.database.models import Topic

    group = await crud.get_group_by_chat_id(session, message.chat.id)
    if not group:
        return

    topic = Topic(
        group_id=group.id,
        chat_id=message.chat.id,
        message_thread_id=message.message_thread_id,
        topic_name=message.forum_topic_created.name,
        topic_type="ignored",  # Default to ignored until linked
        status="active"
    )
    await crud.create(session, topic)
    logger.info("topic_created_registered", topic_name=message.forum_topic_created.name)


@router.message(F.forum_topic_edited)
async def handle_topic_edited(message: types.Message, session: AsyncSession) -> None:
    """Update topic name if edited."""
    from app.database import crud
    from app.database.models import Topic
    
    if not message.forum_topic_edited.name:
        return
        
    topic = await crud.get_topic(session, message.chat.id, message.message_thread_id)
    if topic:
        await crud.update_fields(session, Topic, topic.id, topic_name=message.forum_topic_edited.name)


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
)
async def handle_group_message(message: types.Message, session: AsyncSession) -> None:
    """Process every message in group chats.

    Handles text, photos (OCR), documents (PDF/DOCX), and voice notes (Whisper).
    """
    if (
        not message.text
        and not message.caption
        and not message.photo
        and not message.document
        and not message.voice
        and not message.audio
        and not message.animation
        and not message.sticker
    ):
        logger.info(
            "group_message_ignored_empty",
            chat_id=message.chat.id,
            thread_id=message.message_thread_id,
            message_id=message.message_id,
        )
        return

    from app.database import crud

    group = await crud.get_group_by_chat_id(session, message.chat.id)
    if not group:
        logger.info(
            "group_message_unregistered_group",
            chat_id=message.chat.id,
            thread_id=message.message_thread_id,
            message_id=message.message_id,
        )
        return

    import uuid
    import os

    chat_id = message.chat.id
    thread_id = message.message_thread_id
    text = message.text or message.caption or ""
    user_id = message.from_user.id if message.from_user else None
    message_id = message.message_id

    # Auto-register unknown topics for backward compatibility
    if thread_id:
        topic = await crud.get_topic(session, chat_id, thread_id)
        if not topic:
            from app.database.models import Topic
            
            # If the user replies to the main topic creation message, it sometimes has the title
            topic_name = f"Topic {thread_id}"
            if message.reply_to_message and message.reply_to_message.forum_topic_created:
                topic_name = message.reply_to_message.forum_topic_created.name
                
            new_topic = Topic(
                group_id=group.id,
                chat_id=chat_id,
                message_thread_id=thread_id,
                topic_name=topic_name,
                topic_type="ignored",
                status="active"
            )
            await crud.create(session, new_topic)
            logger.info(
                "topic_auto_registered",
                group_id=group.id,
                chat_id=chat_id,
                thread_id=thread_id,
                topic_name=topic_name,
            )

    os.makedirs("app/storage/raw", exist_ok=True)

    # helper for downloading files
    async def _download_and_save(file_path_obj, prefix: str, ext: str) -> bytes | None:
        try:
            file_io = await message.bot.download(file_path_obj)
            if hasattr(file_io, "read"):
                file_bytes = file_io.read()
                raw_filename = f"app/storage/raw/{prefix}_{message_id}_{uuid.uuid4().hex[:8]}.{ext}"
                with open(raw_filename, "wb") as f:
                    f.write(file_bytes)
                return file_bytes
        except Exception:
            logger.exception(f"{prefix}_processing_failed", message_id=message_id)
        return None

    # Enable safety filtering
    if getattr(group, "ai_moderation_enabled", False):
        from app.ai.safety_client import safety_client

        media_obj = None
        ext = "jpg"
        mime = "image/jpeg"

        if message.photo:
            media_obj = message.photo[-1]
        elif message.animation:
            media_obj = message.animation
            ext = "mp4"
            mime = "video/mp4"
        elif message.sticker:
            media_obj = message.sticker
            if media_obj.is_animated and not media_obj.is_video:
                media_obj = None  # Skip TGS json files
            else:
                ext = "webm" if media_obj.is_video else "webp"
                mime = "video/webm" if media_obj.is_video else "image/webp"

        if media_obj:
            media_bytes = await _download_and_save(media_obj, f"mod_{ext}", ext)
            if media_bytes:
                is_safe = await safety_client.is_safe(media_bytes, mime)
                if not is_safe:
                    try:
                        await message.delete()
                        await crud.log_action(
                            session,
                            "moderation_deleted",
                            chat_id=chat_id,
                            details=f"user={user_id} type={ext}",
                        )
                    except Exception:
                        pass
                    return

    # If it's JUST an animation or sticker and passed moderation, we ignore it for academic routing
    if message.animation or message.sticker:
        if not text:
            logger.info(
                "group_message_ignored_media_only",
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
            )
            return

    # Process images with OCR
    if message.photo:
        from app.ocr.engine import extract_text_from_image

        photo = message.photo[-1]  # Highest resolution
        image_bytes = await _download_and_save(photo, "photo", "jpg")
        if image_bytes:
            ocr_text = await extract_text_from_image(image_bytes)
            if ocr_text:
                logger.info("ocr_completed_for_message", message_id=message_id)
                text = f"{text}\n\n[OCR Data]:\n{ocr_text}".strip()

    # Process Documents
    if message.document:
        from app.files.parser import extract_text_from_pdf, extract_text_from_docx

        doc = message.document
        ext = doc.file_name.split(".")[-1].lower() if doc.file_name else "bin"
        if ext in ("pdf", "docx", "doc"):
            doc_bytes = await _download_and_save(doc, "doc", ext)
            if doc_bytes:
                extracted = None
                if ext == "pdf":
                    extracted = await extract_text_from_pdf(doc_bytes)
                elif ext in ("docx", "doc"):
                    extracted = await extract_text_from_docx(doc_bytes)

                if extracted:
                    logger.info("doc_parsed_for_message", message_id=message_id)
                    text = f"{text}\n\n[Document Data]:\n{extracted}".strip()

    # Process Voice/Audio
    audio_obj = message.voice or message.audio
    if audio_obj:
        from app.ai.groq_client import groq_client

        ext = (
            "ogg"
            if message.voice
            else ("mp3" if audio_obj.file_name and audio_obj.file_name.endswith("mp3") else "audio")
        )
        audio_bytes = await _download_and_save(audio_obj, "audio", ext)
        if audio_bytes:
            transcript = await groq_client.transcribe_audio(audio_bytes, f"audio.{ext}")
            if transcript:
                logger.info("voice_transcribed_for_message", message_id=message_id)
                text = f"{text}\n\n[Audio Transcript]:\n{transcript}".strip()

    if not text:
        logger.info(
            "group_message_ignored_no_extracted_text",
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
        )
        return

    logger.info(
        "group_message",
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        text_length=len(text),
        has_media=bool(message.photo or message.document or audio_obj),
    )

    await process_group_message(
        session=session,
        chat_id=chat_id,
        thread_id=thread_id,
        text=text,
        user_id=user_id,
        message_id=message_id,
        is_document=bool(message.document)
    )
