"""Service for sending automated weekly digests of upcoming academic items."""

from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram import Bot

from app.database import crud
from app.database.models import AcademicItem, Group, Course
from app.utils.timezone import now_addis
from app.logging import get_logger

logger = get_logger("weekly_digest")

async def send_weekly_digest(bot: Bot) -> None:
    """Send a weekly compilation of upcoming deadlines to all active groups."""
    from app.database.session import async_session_factory
    
    now = now_addis().replace(tzinfo=None)
    next_week = now + timedelta(days=7)
    
    async with async_session_factory() as session:
        async with session.begin():
            # Get all active groups
            stmt = select(Group).where(Group.active == True)
            result = await session.execute(stmt)
            groups = result.scalars().all()
            
            for group in groups:
                # Find all upcoming items within the next 7 days for this group
                item_stmt = (
                    select(AcademicItem, Course)
                    .outerjoin(Course, AcademicItem.course_id == Course.id)
                    .where(
                        AcademicItem.group_id == group.id,
                        AcademicItem.deadline >= now,
                        AcademicItem.deadline <= next_week,
                        AcademicItem.status == 'approved' # Only show formally approved items
                    )
                    .order_by(AcademicItem.deadline.asc())
                )
                
                item_res = await session.execute(item_stmt)
                rows = item_res.all()
                
                if not rows:
                    continue
                
                # Format Digest
                lines = [
                    "📅 <b>Weekly Academic Digest</b>",
                    "Here's what you have coming up this week:\n"
                ]
                
                for item, course in rows:
                    cname = course.course_name if course else "General"
                    d_str = item.deadline.strftime("%a, %b %d at %I:%M %p")
                    lines.append(f"• <b>{cname} {item.item_type.title()}</b>\n   ⏳ <i>{d_str}</i>")
                    if item.title:
                        lines.append(f"   ↳ {item.title}")
                
                lines.append("\nHave a great week! 🚀")
                text = "\n".join(lines)
                
                # Send to general topic
                general_topic = await crud.get_general_topic(session, group.id)
                if general_topic:
                    thread_id = general_topic.message_thread_id if general_topic.message_thread_id and general_topic.message_thread_id > 0 else None
                    try:
                        await bot.send_message(
                            chat_id=general_topic.chat_id,
                            message_thread_id=thread_id,
                            text=text,
                            parse_mode="HTML"
                        )
                        logger.info("weekly_digest_sent", group_id=group.id)
                    except Exception as e:
                        logger.warning("weekly_digest_failed", group_id=group.id, error=str(e))
