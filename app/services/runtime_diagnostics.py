"""Runtime diagnostics for live Telegram operations."""

from __future__ import annotations

import html
from typing import Any

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import redis_cache
from app.config import settings
from app.database.models import AcademicItem, Course, Group, Reminder, Topic


async def collect_runtime_diagnostics(
    *,
    bot: Bot,
    session: AsyncSession,
    fsm_storage: Any,
) -> dict[str, Any]:
    """Collect runtime status from Telegram, Redis, DB, and scheduler."""
    me = await bot.get_me()
    webhook = await bot.get_webhook_info()

    cache_connected = False
    if redis_cache._redis is not None:
        try:
            cache_connected = bool(await redis_cache._redis.ping())
        except Exception:
            cache_connected = False

    fsm_connected = False
    fsm_redis = getattr(fsm_storage, "redis", None)
    if fsm_redis is not None:
        try:
            fsm_connected = bool(await fsm_redis.ping())
        except Exception:
            fsm_connected = False

    group_count = await _count(session, Group)
    topic_count = await _count(session, Topic)
    course_count = await _count(session, Course)
    item_count = await _count(session, AcademicItem)
    reminder_count = await _count(session, Reminder)

    groups = []
    result = await session.execute(select(Group).where(Group.active == True).order_by(Group.id))
    for group in result.scalars().all():
        topics = []
        topic_result = await session.execute(
            select(Topic, Course)
            .outerjoin(Course, Course.topic_id == Topic.id)
            .where(Topic.group_id == group.id)
            .order_by(Topic.id)
        )
        for topic, course in topic_result.all():
            label = topic.topic_name
            if course:
                label = f"{topic.topic_name} -> {course.course_name}"
            topics.append(f"{label} [thread={topic.message_thread_id} status={topic.status}]")

        groups.append(
            {
                "id": group.id,
                "chat_id": group.chat_id,
                "label": f"{group.department or 'Unknown'} Y{group.year or '?'} {group.section or ''}".strip(),
                "topics": topics,
            }
        )

    from app.reminders.scheduler import scheduler

    jobs = [
        f"{job.id} -> {job.next_run_time.isoformat() if job.next_run_time else 'none'}"
        for job in scheduler.get_jobs()
    ]

    return {
        "bot": {
            "username": me.username,
            "can_read_all_group_messages": getattr(me, "can_read_all_group_messages", None),
            "can_join_groups": getattr(me, "can_join_groups", None),
        },
        "telegram": {
            "mode": "polling" if settings.use_polling else "webhook",
            "webhook_url_set": bool(webhook.url),
            "pending_update_count": webhook.pending_update_count,
            "last_error": webhook.last_error_message,
        },
        "redis": {
            "cache_connected": cache_connected,
            "fsm_connected": fsm_connected,
            "storage_type": type(fsm_storage).__name__,
        },
        "database": {
            "ok": True,
            "groups": group_count,
            "topics": topic_count,
            "courses": course_count,
            "academic_items": item_count,
            "reminders": reminder_count,
        },
        "scheduler": {
            "running": scheduler.running,
            "job_count": len(jobs),
            "jobs": jobs[:10],
        },
        "groups": groups[:10],
    }


async def _count(session: AsyncSession, model: type) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar() or 0)


def render_runtime_diagnostics(report: dict[str, Any]) -> str:
    """Render diagnostics as Telegram-safe HTML."""
    bot = report["bot"]
    telegram = report["telegram"]
    redis = report["redis"]
    database = report["database"]
    scheduler = report["scheduler"]

    lines = [
        "<b>Runtime Diagnostics</b>",
        "",
        "<b>Bot</b>",
        f"@{html.escape(str(bot['username']))}",
        f"<code>can_read_all_group_messages={bot['can_read_all_group_messages']}</code>",
        f"<code>can_join_groups={bot['can_join_groups']}</code>",
        "",
        "<b>Telegram</b>",
        f"<code>mode={html.escape(str(telegram['mode']))}</code>",
        f"<code>webhook_url_set={telegram['webhook_url_set']}</code>",
        f"<code>pending_update_count={telegram['pending_update_count']}</code>",
        f"<code>last_error={html.escape(str(telegram.get('last_error') or 'none'))}</code>",
        "",
        "<b>Redis / FSM</b>",
        f"<code>cache_connected={redis['cache_connected']}</code>",
        f"<code>fsm_connected={redis['fsm_connected']}</code>",
        f"<code>storage={html.escape(str(redis['storage_type']))}</code>",
        "",
        "<b>Database</b>",
        (
            f"<code>groups={database['groups']} topics={database['topics']} "
            f"courses={database['courses']}</code>"
        ),
        (
            f"<code>academic_items={database['academic_items']} "
            f"reminders={database['reminders']}</code>"
        ),
        "",
        "<b>Scheduler</b>",
        f"<code>running={scheduler['running']} jobs={scheduler['job_count']}</code>",
    ]

    for job in scheduler["jobs"]:
        lines.append(f"<code>{html.escape(str(job))}</code>")

    lines.extend(["", "<b>Groups / Topics</b>"])
    for group in report["groups"]:
        lines.append(
            f"<code>{group['id']} chat={group['chat_id']} {html.escape(str(group['label']))}</code>"
        )
        for topic in group["topics"][:8]:
            lines.append(f"<code>{html.escape(str(topic))}</code>")

    return "\n".join(lines)
