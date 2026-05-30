"""Academic Item service — manages operations on academic items like semantic duplicate detection."""

from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AcademicItem
from app.logging import get_logger

logger = get_logger("academic_item_service")


async def is_semantic_duplicate(
    session: AsyncSession,
    group_id: int,
    course_id: int | None,
    item_type: str,
    deadline: str | None,
    title: str | None = None,
) -> AcademicItem | None:
    """Check if an exact or very similar academic item already exists.

    Returns the duplicate item if found, else None.
    """
    if not deadline:
        # For items without a deadline, we only check within the last few hours
        # or we skip duplicate detection (safest to skip to avoid missing important info)
        return None

    # Look for active items of the same type, same course, and similar deadline (+- 24 hours)
    stmt = select(AcademicItem).where(
        AcademicItem.group_id == group_id,
        AcademicItem.item_type == item_type,
        AcademicItem.course_id == course_id,
        AcademicItem.status.in_({"new", "active", "verified"}),
    )
    result = await session.execute(stmt)
    active_items = result.scalars().all()

    import difflib

    for existing in active_items:
        if existing.deadline:
            # Check if deadline is within 48 hours
            diff = abs(existing.deadline - deadline)
            if diff <= timedelta(hours=48):
                # Also verify title similarity (if it's a completely different assignment, don't drop it!)
                if title and existing.title:
                    new_title = title.lower()
                    existing_title = existing.title.lower()
                    subtype_words = {"midterm", "final", "quiz", "project", "presentation", "lab"}
                    new_subtypes = {word for word in subtype_words if word in new_title}
                    existing_subtypes = {word for word in subtype_words if word in existing_title}
                    if new_subtypes != existing_subtypes:
                        continue
                    similarity = difflib.SequenceMatcher(None, title.lower(), existing.title.lower()).ratio()
                    if similarity < 0.5:
                        continue  # Titles are too different, probably distinct assignments
                
                logger.info(
                    "duplicate_item_detected",
                    new_item_type=item_type,
                    existing_id=existing.id,
                )
                return existing

    return None
