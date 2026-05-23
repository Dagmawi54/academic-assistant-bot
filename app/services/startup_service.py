"""Diagnostic and reconciliation routines run on application startup."""

import asyncio
from aiogram import Bot
from sqlalchemy import select

from app.database.session import async_session_factory
from app.database.models import Group, Topic, Course
from app.logging import get_logger

logger = get_logger("startup")


async def run_startup_diagnostics(bot: Bot) -> None:
    """Verifies that all groups and topics persist correctly across cold starts."""
    try:
        async with async_session_factory() as session:
            # 1. Fetch all active groups
            stmt = select(Group).where(Group.active == True)
            result = await session.execute(stmt)
            groups = result.scalars().all()
            
            if not groups:
                logger.info("startup_diagnostics", msg="No active groups found. Awaiting registrations.")
                return
                
            logger.info("startup_diagnostics", msg=f"Found {len(groups)} active groups. Verifying topic mappings...")
            
            for group in groups:
                try:
                    # 2. Check if the Telegram group still exists and is accessible
                    chat = await bot.get_chat(group.chat_id)
                    logger.info(
                        "group_verified", 
                        group_id=group.id, 
                        department=group.department, 
                        telegram_title=chat.title
                    )
                    
                    # 3. Enumerate all stored topics for this group
                    t_stmt = select(Topic).where(Topic.group_id == group.id, Topic.status == "active")
                    t_result = await session.execute(t_stmt)
                    topics = t_result.scalars().all()
                    
                    for topic in topics:
                        # 4. Check if course is linked
                        if topic.topic_type == "course":
                            c_stmt = select(Course).where(Course.topic_id == topic.id, Course.active == True)
                            c_result = await session.execute(c_stmt)
                            course = c_result.scalar_one_or_none()
                            
                            logger.info(
                                "topic_mapping", 
                                group=group.id,
                                topic_id=topic.id,
                                topic_name=topic.topic_name,
                                linked_course=course.course_name if course else "ORPHANED"
                            )
                        else:
                            logger.info(
                                "topic_mapping", 
                                group=group.id,
                                topic_id=topic.id,
                                topic_name=topic.topic_name,
                                type=topic.topic_type
                            )
                
                except Exception as e:
                    logger.error("group_verification_failed", group_id=group.id, error=str(e))
                    
    except Exception as e:
        logger.error("startup_diagnostics_failed", error=str(e))
