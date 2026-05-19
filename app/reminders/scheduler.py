"""APScheduler setup with SQLAlchemy job store for persistence."""

from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from app.config import settings
from app.logging import get_logger

logger = get_logger("scheduler")

# Use synchronous SQLite URL for APScheduler (it handles its own connections)
_job_store_url = settings.database_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg2")

scheduler = AsyncIOScheduler(
    jobstores={
        "default": SQLAlchemyJobStore(url=_job_store_url),
    },
    job_defaults={
        "coalesce": True,  # Merge missed runs into one
        "max_instances": 1,
        "misfire_grace_time": 3600,  # Allow 1 hour of delay
    },
    timezone="Africa/Addis_Ababa",
)


async def send_reminder(reminder_id: int) -> None:
    """Send a scheduled reminder notification."""
    from app.database.session import async_session_factory
    from app.database import crud
    from app.database.models import AcademicItem, Reminder
    from app.bot import bot
    from app.reminders.formatter import format_reminder
    from app.utils.timezone import now_addis

    async with async_session_factory() as session:
        async with session.begin():
            reminder = await crud.get_by_id(session, Reminder, reminder_id)
            if not reminder or reminder.sent or reminder.cancelled:
                return

            item = await crud.get_by_id(session, AcademicItem, reminder.item_id)
            if not item:
                return

            text = format_reminder(item)

            try:
                from app.metrics.tracker import tracker

                await tracker.record_reminder(success=False)

                await bot.send_message(
                    chat_id=reminder.chat_id,
                    message_thread_id=reminder.thread_id,
                    text=text,
                )
                await tracker.record_reminder(success=True)

                await crud.update_fields(
                    session,
                    Reminder,
                    reminder.id,
                    sent=True,
                    sent_at=now_addis(),
                )
                logger.info("reminder_sent", reminder_id=reminder_id, item_id=item.id)
            except Exception:
                logger.exception("reminder_send_failed", reminder_id=reminder_id)


async def start_scheduler() -> None:
    """Start the scheduler and rebuild reminder jobs from DB."""
    from app.database.session import async_session_factory
    from app.database import crud
    from app.utils.timezone import now_addis

    # Rebuild pending reminders as jobs
    async with async_session_factory() as session:
        async with session.begin():
            pending = await crud.get_pending_reminders(session)
            now = now_addis()
            scheduled = 0

            for reminder in pending:
                if reminder.send_time > now:
                    job_id = f"reminder_{reminder.id}"
                    # Avoid duplicate jobs
                    if not scheduler.get_job(job_id):
                        scheduler.add_job(
                            send_reminder,
                            "date",
                            run_date=reminder.send_time,
                            args=[reminder.id],
                            id=job_id,
                            replace_existing=True,
                        )
                        scheduled += 1

            logger.info("scheduler_rebuild", pending=len(pending), scheduled=scheduled)

    scheduler.start()
    logger.info("scheduler_started")


def stop_scheduler() -> None:
    """Shut down the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")


def schedule_reminder(reminder_id: int, send_time: datetime) -> None:
    """Schedule a single reminder job."""
    job_id = f"reminder_{reminder_id}"
    scheduler.add_job(
        send_reminder,
        "date",
        run_date=send_time,
        args=[reminder_id],
        id=job_id,
        replace_existing=True,
    )
    logger.info("reminder_scheduled", reminder_id=reminder_id, send_time=str(send_time))
