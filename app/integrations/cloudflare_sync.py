"""Durable post-commit mirroring of academic items into the Cloudflare D1 queue."""

import json
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import AcademicItem, CloudflareSyncOutbox, Reminder
from app.database.session import async_session_factory
from app.logging import get_logger
from app.utils.timezone import to_addis

logger = get_logger("cloudflare_sync")
SYNCABLE_ITEM_TYPES = {"assignment", "exam", "quiz"}


def _timestamp_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(to_addis(value).timestamp() * 1000)


def serialize_academic_item(item: AcademicItem, reminders: list[Reminder]) -> dict:
    item_status = item.status.lower()
    if item_status == "approved":
        item_status = "active"
    elif item_status == "rejected":
        item_status = "archived"
    return {
        "source_item_id": item.id,
        "group_id": item.group_id,
        "chat_id": str(item.source_chat_id or 0),
        "thread_id": reminders[0].thread_id if reminders else None,
        "item_type": item.item_type.lower(),
        "title": item.title,
        "deadline_ms": _timestamp_ms(item.deadline),
        "room": item.room,
        "coverage": item.coverage,
        "status": item_status,
        "reminders": [
            {
                "source_reminder_id": reminder.id,
                "send_time_ms": _timestamp_ms(reminder.send_time),
                "sent": reminder.sent,
                "cancelled": reminder.cancelled,
            }
            for reminder in reminders
        ],
    }


async def flush_queued_items(session: AsyncSession) -> None:
    """Deliver due durable sync events after the originating transaction commits."""
    item_ids = session.info.pop("cloudflare_academic_item_ids", set())
    if not item_ids or not settings.cloudflare_worker_url or not settings.cloudflare_sync_secret:
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await session.execute(
        select(CloudflareSyncOutbox)
        .where(
            CloudflareSyncOutbox.academic_item_id.in_(item_ids),
            CloudflareSyncOutbox.status == "pending",
            CloudflareSyncOutbox.next_attempt_at <= now,
        )
        .order_by(CloudflareSyncOutbox.created_at)
        .limit(25)
    )
    rows = list(result.scalars().all())
    if not rows:
        return

    succeeded = await _post_items([json.loads(row.payload) for row in rows])
    for row in rows:
        row.attempt_count += 1
        if succeeded:
            row.status = "synced"
            row.synced_at = now
            row.last_error = None
        else:
            retry_seconds = min(30 * (2 ** min(row.attempt_count - 1, 7)), 3600)
            row.next_attempt_at = now + timedelta(seconds=retry_seconds)
            row.last_error = "worker_sync_failed"
    await session.commit()


async def _post_items(items: list[dict]) -> bool:
    url = settings.cloudflare_worker_url.rstrip("/") + "/internal/academic-items"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                headers={"X-Academic-Sync-Secret": settings.cloudflare_sync_secret},
                json={"items": items},
            )
            response.raise_for_status()
        logger.info("cloudflare_academic_items_synced", count=len(items))
        return True
    except httpx.HTTPError:
        logger.exception("cloudflare_academic_item_sync_failed", count=len(items))
        return False


async def retry_pending_cloudflare_syncs() -> None:
    """Retry due outbox rows from the existing scheduler until a sync succeeds."""
    if not settings.cloudflare_worker_url or not settings.cloudflare_sync_secret:
        return
    async with async_session_factory() as session:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        result = await session.execute(
            select(CloudflareSyncOutbox.academic_item_id)
            .where(
                CloudflareSyncOutbox.status == "pending",
                CloudflareSyncOutbox.next_attempt_at <= now,
            )
            .order_by(CloudflareSyncOutbox.created_at)
            .limit(25)
        )
        session.info["cloudflare_academic_item_ids"] = {
            row[0] for row in result.all()
        }
        await flush_queued_items(session)


async def queue_academic_item(session: AsyncSession, item: AcademicItem, reminders: list[Reminder]) -> None:
    """Persist one idempotent mirror event in the caller's academic transaction."""
    if (
        not settings.cloudflare_worker_url
        or not settings.cloudflare_sync_secret
        or item.item_type.lower() not in SYNCABLE_ITEM_TYPES
    ):
        return
    result = await session.execute(
        select(CloudflareSyncOutbox).where(CloudflareSyncOutbox.academic_item_id == item.id)
    )
    row = result.scalar_one_or_none()
    payload = json.dumps(serialize_academic_item(item, reminders))
    if row is None:
        row = CloudflareSyncOutbox(academic_item_id=item.id, payload=payload)
        session.add(row)
    else:
        row.payload = payload
        row.status = "pending"
        row.attempt_count = 0
        row.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.last_error = None
        row.synced_at = None
    session.info.setdefault("cloudflare_academic_item_ids", set()).add(item.id)
