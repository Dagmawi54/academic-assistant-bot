"""AI Quiz Engine for generating educational Telegram Polls from study materials."""

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram import Bot

from app.database import crud
from app.database.models import AcademicItem, Group, Course, Topic
from app.ai.academic_extraction_client import academic_extraction_client
from app.logging import get_logger

logger = get_logger("quiz_engine")

QUIZ_PROMPT = """
You are an expert AI academic tutor. Your task is to review the following study material and generate exactly {num_questions} multiple choice questions to test the students.
Rules:
1. Generate EXACTLY {num_questions} questions.
2. Each question MUST have exactly 4 short options.
3. Keep the questions focused tightly on the material provided.

Output ONLY valid JSON matching this exact structure:
{
    "questions": [
        {
            "question": "What is the capital of France?",
            "options": ["Paris", "London", "Berlin", "Rome"],
            "correct_index": 0
        }
    ]
}
"""

async def fetch_quiz_topic(session: AsyncSession, group_id: int) -> Topic | None:
    """Finds or resolves the 'Quiz' topic for the group."""
    stmt = select(Topic).where(
        Topic.group_id == group_id,
        Topic.topic_name.ilike("%quiz%"),
        Topic.status == "active"
    )
    result = await session.execute(stmt)
    quiz_topic = result.scalars().first()
    if quiz_topic:
        return quiz_topic
    
    # Fallback to general topic
    return await crud.get_general_topic(session, group_id)

async def generate_quiz_for_course(session: AsyncSession, course_id: int, num_questions: int = 5) -> list[dict[str, Any]] | None:
    """Extract material texts and prompt Groq to build a JSON quiz."""
    stmt = (
        select(AcademicItem)
        .where(
            AcademicItem.course_id == course_id,
            AcademicItem.item_type == "MATERIAL",
            AcademicItem.status != "archived"
        )
    )
    result = await session.execute(stmt)
    materials = result.scalars().all()
    
    if not materials:
        return None
        
    material_texts = [m.raw_text.strip() for m in materials if m.raw_text and len(m.raw_text.strip()) > 50]
    if not material_texts:
        return None
        
    # Combine texts, limit to a reasonable context window (e.g. roughly 15000 chars)
    combined_text = "\n\n---\n\n".join(material_texts)[:18000]
    prompt = QUIZ_PROMPT.replace("{num_questions}", str(num_questions))
    
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": combined_text}
    ]
    
    response = await academic_extraction_client.complete_json(
        messages,
        response_format={"type": "json_object"}
    )
    
    if not response or "questions" not in response:
        logger.warning("quiz_generation_failed", course_id=course_id, reason="invalid_json_format")
        return None
        
    questions = response["questions"]
    if not isinstance(questions, list) or len(questions) == 0:
        logger.warning("quiz_generation_failed", course_id=course_id, reason="empty_questions_list")
        return None
        
    return questions

async def broadcast_quiz(bot: Bot, session: AsyncSession, course: Course, questions: list[dict[str, Any]]) -> None:
    """Broadcasts a series of poll questions sequentially to the group's Quiz topic."""
    target_topic = await fetch_quiz_topic(session, course.group_id)
    if not target_topic:
        logger.warning("quiz_broadcast_aborted", course_id=course.id, reason="no_target_topic")
        return
        
    # Send a header message first
    thread_id = target_topic.message_thread_id if target_topic.message_thread_id and target_topic.message_thread_id > 0 else None
    
    try:
        await bot.send_message(
            chat_id=target_topic.chat_id,
            message_thread_id=thread_id,
            text=f"🧠 <b>Pop Quiz Time: {course.course_name}</b>\n\nTesting your knowledge based on recent study materials. Good luck!",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception("quiz_header_failed", course_id=course.id, error=str(e))
        return

    # Send polls with a slight delay
    success_count = 0
    for q in questions:
        await asyncio.sleep(2)
        try:
            options = q.get("options", [])
            correct_idx = q.get("correct_index", 0)
            
            # Defensive check
            if len(options) < 2 or correct_idx < 0 or correct_idx >= len(options):
                continue
                
            await bot.send_poll(
                chat_id=target_topic.chat_id,
                message_thread_id=thread_id,
                question=str(q.get("question", "Question"))[:300], # Telegram limit is 300
                options=[str(opt)[:100] for opt in options],       # Telegram limit is 100 per option
                type="quiz",
                correct_option_id=correct_idx,
                is_anonymous=True, # Quizzes must typically be anonymous in groups
            )
            success_count += 1
        except Exception as e:
            logger.exception("quiz_poll_failed", course_id=course.id, error=str(e))
            
    logger.info("quiz_broadcast_completed", course_id=course.id, questions_sent=success_count)


async def cron_bi_daily_quiz(bot: Bot) -> None:
    """Cron wrapper for pulling a random active course and broadcasting a 5-question quiz."""
    from app.database.session import async_session_factory
    import random
    
    async with async_session_factory() as session:
        async with session.begin():
            result = await session.execute(select(Group).where(Group.active == True))
            groups = result.scalars().all()
            
            for group in groups:
                 c_res = await session.execute(select(Course).where(Course.group_id == group.id))
                 courses = c_res.scalars().all()
                 if not courses:
                     continue
                 
                 course = random.choice(courses)
                 questions = await generate_quiz_for_course(session, course.id, num_questions=5)
                 if questions:
                     await broadcast_quiz(bot, session, course, questions)

async def cron_daily_exam_prep_quiz(bot: Bot) -> None:
    """Cron wrapper that checks for exams tomorrow and kicks off 20-question review surges."""
    from app.database.session import async_session_factory
    from datetime import timedelta
    from app.utils.timezone import now_addis
    
    now = now_addis().replace(tzinfo=None)
    tomorrow_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_end = tomorrow_start + timedelta(days=1)
    
    async with async_session_factory() as session:
        async with session.begin():
            stmt = select(AcademicItem, Course).join(Course, AcademicItem.course_id == Course.id).where(
                AcademicItem.item_type == "EXAM",
                AcademicItem.deadline >= tomorrow_start,
                AcademicItem.deadline < tomorrow_end,
                AcademicItem.status != "archived"
            )
            result = await session.execute(stmt)
            rows = result.all()
            
            handled_courses = set()
            for item, course in rows:
                if course.id in handled_courses:
                    continue
                handled_courses.add(course.id)
                logger.info("exam_surge_triggered", course_id=course.id, exam_date=str(item.deadline))
                
                questions = await generate_quiz_for_course(session, course.id, num_questions=20)
                if questions:
                    await broadcast_quiz(bot, session, course, questions)
