"""Service to export academic items as an iCalendar (.ics) file."""

import uuid
from datetime import datetime, timedelta, timezone

from aiogram.types import BufferedInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AcademicItem, Course, Group
from app.utils.timezone import now_addis


async def generate_calendar_for_group(session: AsyncSession, group_id: int) -> BufferedInputFile | None:
    """Generate a `.ics` file containing all future academic events for a group."""
    now = now_addis().replace(tzinfo=None)
    
    stmt = (
        select(AcademicItem, Course)
        .outerjoin(Course, AcademicItem.course_id == Course.id)
        .where(
            AcademicItem.group_id == group_id,
            AcademicItem.deadline >= now,
            AcademicItem.status != "archived"
        )
    )
    result = await session.execute(stmt)
    rows = result.all()
    
    if not rows:
        return None

    group = await session.get(Group, group_id)
    dept_name = group.department or "Unknown"
    group_name = f"{dept_name} Y{group.year} S{group.section}"

    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Academic Group Bot//EN",
        f"X-WR-CALNAME:Deadlines - {group_name}"
    ]

    for item, course in rows:
        course_name = course.course_name if course else "General"
        uid = f"{item.id}-{uuid.uuid4().hex[:8]}@academicbot"
        
        deadline_dt = item.deadline
        start_dt = deadline_dt - timedelta(hours=1)
        
        deadline_str = deadline_dt.strftime("%Y%m%dT%H%M%S")
        start_str = start_dt.strftime("%Y%m%dT%H%M%S")
        
        title = item.title or f"{course_name} {item.item_type.title()}"
        summary = f"[{course_name}] {title}"
        
        desc = item.raw_text or item.coverage or "No description provided."
        desc = desc.replace("\n", "\\n").replace("\r", "")
        
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART:{start_str}",
            f"DTEND:{deadline_str}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            "END:VEVENT"
        ])

    lines.append("END:VCALENDAR")
    
    ics_content = "\r\n".join(lines).encode("utf-8")
    
    filename = f"deadlines_{group.department or 'dept'}_y{group.year}_s{group.section}.ics".replace(" ", "_").lower()
    return BufferedInputFile(ics_content, filename=filename)
