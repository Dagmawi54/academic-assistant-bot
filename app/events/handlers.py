"""Event listeners — connect events to services."""

from app.events.bus import on, ASSIGNMENT_DETECTED, EXAM_DETECTED, ITEM_UPDATED, SEMESTER_CLOSED
from app.logging import get_logger

logger = get_logger("event_handlers")


@on(ASSIGNMENT_DETECTED)
async def on_assignment_detected(*, item_id: int, **kwargs) -> None:  # type: ignore[no-untyped-def]
    """Create reminders when a new assignment is detected."""
    if kwargs.get("reminders_already_created"):
        logger.info("reminders_already_created", item_id=item_id, trigger="assignment_detected")
        return

    # Import here to avoid circular imports at module level
    from app.services.reminder_service import create_reminders_for_item

    logger.info("creating_reminders", item_id=item_id, trigger="assignment_detected")
    await create_reminders_for_item(item_id)


@on(EXAM_DETECTED)
async def on_exam_detected(*, item_id: int, **kwargs) -> None:  # type: ignore[no-untyped-def]
    """Create reminders when an exam is detected."""
    if kwargs.get("reminders_already_created"):
        logger.info("reminders_already_created", item_id=item_id, trigger="exam_detected")
        return

    from app.services.reminder_service import create_reminders_for_item

    logger.info("creating_reminders", item_id=item_id, trigger="exam_detected")
    await create_reminders_for_item(item_id)


@on(ITEM_UPDATED)
async def on_item_updated(*, item_id: int, **kwargs) -> None:  # type: ignore[no-untyped-def]
    """Cancel old reminders and create new ones when an item is updated."""
    from app.services.reminder_service import recreate_reminders_for_item

    logger.info("recreating_reminders", item_id=item_id, trigger="item_updated")
    await recreate_reminders_for_item(item_id)


@on(SEMESTER_CLOSED)
async def on_semester_closed(*, group_id: int, **kwargs) -> None:  # type: ignore[no-untyped-def]
    """Cancel all pending reminders when a semester is closed."""
    from app.services.reminder_service import cancel_group_reminders

    logger.info("cancelling_group_reminders", group_id=group_id, trigger="semester_closed")
    await cancel_group_reminders(group_id)
