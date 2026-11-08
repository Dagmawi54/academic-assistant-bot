"""Cloudflare Worker for the Telegram academic assistant pilot."""

import asyncio
import batch_exercises
import hashlib
import hmac
import json
import logging
import re
import followups
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from html import unescape as html_unescape
from typing import Literal
from urllib.parse import quote

import course_quiz_service
import course_tutor_ai
import course_tutor_service
import group_setup_service
import lesson_page_service
import personal_task_service
import poll_service
import quiz_service
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from workers import WorkerEntrypoint

app = FastAPI()
logger = logging.getLogger("academic_assistant_worker")
REMINDER_BATCH_SIZE = 5
DIGEST_BATCH_SIZE = 5
REMINDER_LEASE_MS = 60_000
REMINDER_MAX_ATTEMPTS = 5
REMINDER_RETRY_BASE_MS = 30_000
REMINDER_RETRY_MAX_MS = 3_600_000
ADDIS_OFFSET = timedelta(hours=3)
ACADEMIC_REMINDER_OFFSETS_HOURS = (48, 24, 0)
AI_AUTO_APPROVE_THRESHOLD = 0.90
BOT_COMMAND_MENU_READY = False


@dataclass(frozen=True)
class PersonalTaskReply:
    text: str
    reply_markup: dict | None = None


@dataclass(frozen=True)
class TelegramReply:
    text: str
    reply_markup: dict | None = None


class TelegramMessageRejected(RuntimeError):
    """Telegram returned a definite API rejection for a message."""


class ReminderSync(BaseModel):
    """Reminder data mirrored from the application's academic database."""

    source_reminder_id: int = Field(gt=0)
    send_time_ms: int = Field(ge=0)
    sent: bool = False
    cancelled: bool = False


class AcademicItemSync(BaseModel):
    """Academic item and its reminder schedule from the existing bot."""

    source_item_id: int = Field(gt=0)
    group_id: int
    chat_id: str = Field(min_length=1, max_length=32)
    thread_id: int | None = Field(default=None, gt=0)
    item_type: Literal["assignment", "exam", "quiz"]
    title: str | None = Field(default=None, max_length=500)
    course_name: str | None = Field(default=None, max_length=255)
    deadline_ms: int | None = Field(default=None, ge=0)
    room: str | None = Field(default=None, max_length=100)
    coverage: str | None = Field(default=None, max_length=4000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: Literal["new", "verified", "active", "completed", "archived"] = "active"
    reminders: list[ReminderSync] = Field(default_factory=list, max_length=20)


class AcademicItemSyncBatch(BaseModel):
    """Bounded batch keeps the authenticated sync endpoint predictable."""

    items: list[AcademicItemSync] = Field(min_length=1, max_length=50)


class CollegeProvision(BaseModel):
    """One-time trusted bootstrap of a college and its group invite code."""

    college_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    college_name: str = Field(min_length=2, max_length=200)
    invite_code: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    remaining_uses: int = Field(default=100, ge=1, le=1000)


def parse_academic_command(text: str) -> dict | None:
    """Parse an explicit class-rep event command with an Addis Ababa deadline.

    Accepted examples: `/assignment Database HW 2 | 2026-11-05 14:30` and
    `/exam Midterm | 2026-11-05` (date-only events default to 23:59 local time).
    """
    match = re.fullmatch(
        r"\s*/(assignment|exam|quiz)(?:@\w+)?\s+(.+?)\s*\|\s*"
        r"(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?\s*",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    item_type, title, date_text, time_text = match.groups()
    if not title.strip():
        return None
    try:
        local_deadline = datetime.strptime(
            f"{date_text} {time_text or '23:59'}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone(ADDIS_OFFSET))
    except ValueError:
        return None
    return {
        "item_type": item_type.lower(),
        "title": title.strip(),
        "deadline_ms": int(local_deadline.timestamp() * 1000),
    }


def parse_group_setup_command(text: str) -> dict | None:
    """Parse private group onboarding; invite codes must stay out of group history."""
    match = re.fullmatch(
        r"\s*/group_setup(?:@\w+)?\s+(-100\d{5,})\s+([A-Za-z0-9_-]{16,128})\s*",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return {"chat_id": match.group(1), "invite_code": match.group(2)}


def parse_course_map_command(text: str) -> str | None:
    """Extract a bounded course name from a forum topic mapping command."""
    match = re.fullmatch(r"\s*/course_map(?:@\w+)?\s+(.+?)\s*", text, flags=re.IGNORECASE)
    if not match:
        return None
    course_name = re.sub(r"\s+", " ", match.group(1)).strip()
    return course_name if 2 <= len(course_name) <= 255 else None


def normalize_course_name(name: str) -> str:
    return course_quiz_service.normalize_course_name(name)


def build_reminder_schedule(source_item_id: int, deadline_ms: int, now_ms: int) -> list[dict]:
    """Create stable future reminder IDs for the agreed 48h/24h/deadline offsets."""
    reminders = []
    for index, offset_hours in enumerate(ACADEMIC_REMINDER_OFFSETS_HOURS):
        send_time_ms = deadline_ms - offset_hours * 60 * 60 * 1000
        if send_time_ms > now_ms:
            reminders.append(
                {
                    "source_reminder_id": source_item_id * 10 + index + 1,
                    "send_time_ms": send_time_ms,
                    "sent": False,
                    "cancelled": False,
                }
            )
    return reminders


def is_weekly_digest_window(now_ms: int) -> bool:
    """Prepare digest batches throughout Sunday 18:00–18:59 Addis."""
    addis_now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc) + ADDIS_OFFSET
    return addis_now.weekday() == 6 and addis_now.hour == 18


def weekly_digest_key(now_ms: int) -> str:
    """Return the local Monday date that identifies this academic week."""
    addis_now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc) + ADDIS_OFFSET
    monday = addis_now.date() - timedelta(days=addis_now.weekday())
    return monday.isoformat()


def format_weekly_digest(items: list[dict]) -> str:
    """Render a concise private digest for one subscribed class group."""
    if not items:
        return "<b>This week</b>\n\nNo assignments or exams are due in the next seven days."
    lines = ["<b>This week</b>", ""]
    for item in items:
        label = escape(str(item["item_type"]).title())
        title = escape(item.get("title") or label)
        due = _addis_datetime_text(item["deadline_ms"])
        lines.append(f"• <b>{label}:</b> {title}")
        lines.append(f"  {escape(due)} (Addis Ababa Time)")
        if item.get("item_status") == "new":
            lines.append("  <i>Needs class admin review</i>")
    return "\n".join(lines)


async def register_private_bot_user(database, user_id: int, now_ms: int) -> None:
    """Record the user's private /start so Telegram permits future direct digests."""
    await database.prepare(
        """
        INSERT INTO bot_users (user_id, private_started_at_ms, last_seen_at_ms)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET last_seen_at_ms = excluded.last_seen_at_ms
        """
    ).bind(user_id, now_ms, now_ms).run()


async def set_digest_subscription(
    database,
    user_id: int,
    chat_id: str,
    thread_id: int | None,
    active: bool,
    now_ms: int,
) -> bool:
    """Create or change one student's digest subscription for a group/topic."""
    group = await database.prepare(
        "SELECT chat_id FROM academic_groups WHERE chat_id = ? AND active = 1"
    ).bind(chat_id).first()
    if not group:
        return False
    user = await database.prepare("SELECT user_id FROM bot_users WHERE user_id = ?").bind(user_id).first()
    if not user:
        return False
    scope_key = str(thread_id) if thread_id is not None else "group"
    await database.prepare(
        """
        INSERT INTO digest_subscriptions (
            user_id, chat_id, thread_id, scope_key, active, created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, chat_id, scope_key) DO UPDATE SET
            thread_id = excluded.thread_id,
            active = excluded.active,
            updated_at_ms = excluded.updated_at_ms
        """
    ).bind(user_id, chat_id, thread_id, scope_key, int(active), now_ms, now_ms).run()
    return True


async def _digest_items_for_subscription(database, subscription: dict, now_ms: int) -> list[dict]:
    filters = "i.chat_id = ? AND g.active = 1 AND i.college_id = g.college_id AND i.item_status IN ('new', 'verified', 'active') AND i.deadline_ms BETWEEN ? AND ?"
    values = [subscription["chat_id"], now_ms, now_ms + 7 * 24 * 60 * 60 * 1000]
    if subscription.get("thread_id") is not None:
        filters += " AND i.thread_id = ?"
        values.append(subscription["thread_id"])
    rows = await database.prepare(
        f"SELECT i.item_type, i.title, i.deadline_ms, i.item_status FROM academic_items i JOIN academic_groups g ON g.chat_id = i.chat_id WHERE {filters} ORDER BY i.deadline_ms ASC LIMIT ?"
    ).bind(*values, 12).all()
    return rows["results"] or []


async def enqueue_weekly_digests(database, now_ms: int) -> int:
    """Create weekly digest deliveries idempotently for active opt-in subscriptions."""
    if not is_weekly_digest_window(now_ms):
        return 0
    week_key = weekly_digest_key(now_ms)
    subscriptions = await database.prepare(
        """
        SELECT s.id, s.user_id, s.chat_id, s.thread_id
        FROM digest_subscriptions s
        JOIN academic_groups g ON g.chat_id = s.chat_id AND g.active = 1
        WHERE s.active = 1
          AND NOT EXISTS (
              SELECT 1 FROM weekly_digest_deliveries d
              WHERE d.subscription_id = s.id AND d.week_key = ?
          )
        ORDER BY s.id ASC
        LIMIT ?
        """
    ).bind(week_key, DIGEST_BATCH_SIZE).all()
    queued = 0
    for subscription in subscriptions["results"] or []:
        items = await _digest_items_for_subscription(database, subscription, now_ms)
        message_text = format_weekly_digest(items)
        await database.prepare(
            """
            INSERT OR IGNORE INTO weekly_digest_deliveries (
                subscription_id, week_key, chat_id, message_text, status, created_at_ms
            ) VALUES (?, ?, ?, ?, 'pending', ?)
            """
        ).bind(
            subscription["id"],
            week_key,
            str(subscription["user_id"]),
            message_text,
            now_ms,
        ).run()
        queued += 1
    return queued


async def claim_due_weekly_digests(database, now_ms: int) -> list[dict]:
    """Lease a bounded digest batch for retry-safe Telegram delivery."""
    result = await database.prepare(
        """
        UPDATE weekly_digest_deliveries
        SET status = 'sending', attempt_count = attempt_count + 1,
            lease_until_ms = ?, retry_after_ms = NULL
        WHERE id IN (
            SELECT id FROM weekly_digest_deliveries
            WHERE attempt_count < ? AND (
                status = 'pending'
                OR (status = 'failed' AND retry_after_ms <= ?)
                OR (status = 'sending' AND lease_until_ms <= ?)
            )
            ORDER BY created_at_ms ASC LIMIT ?
        )
        AND (
            status = 'pending'
            OR (status = 'failed' AND retry_after_ms <= ?)
            OR (status = 'sending' AND lease_until_ms <= ?)
        )
        RETURNING id, chat_id, message_text, attempt_count
        """
    ).bind(
        now_ms + REMINDER_LEASE_MS,
        REMINDER_MAX_ATTEMPTS,
        now_ms,
        now_ms,
        DIGEST_BATCH_SIZE,
        now_ms,
        now_ms,
    ).run()
    return result["results"] or []


async def dispatch_claimed_weekly_digests(database, digests: list[dict], send_message, now_ms: int) -> None:
    """Send private digests and persist confirmed delivery or capped retry state."""
    for digest in digests:
        try:
            await send_message(digest)
        except Exception:
            retry_after = now_ms + min(
                REMINDER_RETRY_BASE_MS * (2 ** (digest["attempt_count"] - 1)),
                REMINDER_RETRY_MAX_MS,
            )
            await database.prepare(
                "UPDATE weekly_digest_deliveries SET status = 'failed', retry_after_ms = ?, lease_until_ms = NULL, last_error = ? WHERE id = ? AND status = 'sending'"
            ).bind(retry_after, "telegram_send_failed", digest["id"]).run()
        else:
            await database.prepare(
                "UPDATE weekly_digest_deliveries SET status = 'sent', sent_at_ms = ?, lease_until_ms = NULL, last_error = NULL WHERE id = ? AND status = 'sending'"
            ).bind(now_ms, digest["id"]).run()


def _parse_extracted_deadline(value) -> int | None | bool:
    """Parse an AI ISO date into epoch milliseconds; False signals invalid input."""
    if value in (None, "", "null"):
        return None
    if not isinstance(value, str):
        return False
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            parsed = datetime.strptime(value.strip(), "%Y-%m-%d").replace(hour=23, minute=59)
        else:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(ADDIS_OFFSET))
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return False


def _extracted_text(data: dict, field: str, max_length: int) -> str | None:
    value = data.get(field)
    if field == "coverage" and isinstance(value, dict):
        parts = []
        includes = value.get("includes")
        excludes = value.get("excludes")
        if isinstance(includes, list) and includes:
            parts.append("Includes: " + ", ".join(map(str, includes[:30])))
        if isinstance(excludes, list) and excludes:
            parts.append("Excludes: " + ", ".join(map(str, excludes[:30])))
        if not parts and value.get("raw_statement"):
            parts.append(str(value["raw_statement"]))
        value = " | ".join(parts)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:max_length] if value else None


def parse_ai_academic_extraction(data: dict) -> dict | None:
    """Validate AI output and keep uncertain events in a reviewable state."""
    if not isinstance(data, dict):
        return None
    item_type = str(data.get("type", "")).strip().lower()
    title = data.get("title")
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    deadline_ms = _parse_extracted_deadline(data.get("deadline"))
    if (
        item_type not in {"assignment", "exam", "quiz"}
        or not isinstance(title, str)
        or not title.strip()
        or len(title) > 500
        or not 0 <= confidence <= 1
        or deadline_ms is False
    ):
        return None
    return {
        "item_type": item_type,
        "title": title.strip(),
        "course_name": _extracted_text(data, "course", 255),
        "deadline_ms": deadline_ms,
        "room": _extracted_text(data, "room", 100),
        "coverage": _extracted_text(data, "coverage", 4000),
        "confidence": confidence,
        "status": "active" if confidence >= AI_AUTO_APPROVE_THRESHOLD else "new",
    }


def build_academic_extraction_messages(text: str, now_ms: int) -> list[dict[str, str]]:
    """Create a bounded multilingual extraction prompt using Addis local time."""
    now_local = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc) + ADDIS_OFFSET
    system = (
        "You extract academic events for Ethiopian university Telegram groups. "
        "Understand English, Amharic script, and transliterated Amharic. Resolve relative dates "
        "using the supplied current time. If the date or course is unclear, return null for it. "
        "Never invent a course, date, room, or coverage. Respond with JSON only."
    )
    user = (
        f"Current Addis Ababa date and time: {now_local:%Y-%m-%d %H:%M}.\n"
        "Analyze this message. Return JSON with keys type (assignment, exam, quiz, "
        "exam_coverage, schedule_update, general_notice, discussion, or unknown), course, "
        "deadline (ISO 8601 with timezone or null), room, coverage (string or null), title, "
        f"and confidence (0 to 1).\n\nMessage:\n{text[:8000]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def should_extract_academic_message(text: str) -> bool:
    """Avoid paying for AI on ordinary group chat while retaining Amharic support."""
    normalized = text.casefold()
    signals = (
        "assignment", "asignment", "homework", "submit", "submission", "project",
        "deadline", "due", "exam", "examination", "midterm", "mid-term", "final",
        "quiz", "test", "fetena", "feten", "yemidersbet", "yetimhirt", "coverage",
        "chapter", "schedule", "room", "kefl", "ፈተና", "የቤት ስራ", "ምደባ",
    )
    return len(text.split()) >= 3 and any(signal in normalized for signal in signals)


async def extract_academic_event(api_key: str, text: str, now_ms: int, model: str) -> dict | None:
    """Call Groq using Workers fetch; return only validated academic event data."""
    if not api_key or not text.strip() or not should_extract_academic_message(text):
        return None
    from workers import fetch

    payload = {
        "model": model or "llama-3.1-8b-instant",
        "messages": build_academic_extraction_messages(text, now_ms),
        "temperature": 0,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }
    response = await fetch(
        "https://api.groq.com/openai/v1/chat/completions",
        method="POST",
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        body=json.dumps(payload),
    )
    if response.status >= 400:
        return None
    result = await response.json()
    choices = result.get("choices") or []
    if not choices:
        return None
    try:
        extracted = json.loads(choices[0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return parse_ai_academic_extraction(extracted)


def _resource_message_link(chat: dict, chat_id: str, message_id: int) -> str | None:
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    username = chat.get("username")
    return f"https://t.me/{username}/{message_id}" if username else None


def _extract_media_resource(message: dict, message_link: str, message_id: int, caption: str) -> dict | None:
    for field in ("document", "photo", "video", "audio", "voice"):
        value = message.get(field)
        if not value:
            continue
        media = value[-1] if field == "photo" and isinstance(value, list) else value
        if not isinstance(media, dict) or not isinstance(media.get("file_id"), str):
            continue
        return {
            "source_message_id": message_id,
            "message_link": message_link,
            "thread_id": message.get("message_thread_id"),
            "resource_type": field,
            "title": str(media.get("file_name") or caption or field.title())[:255],
            "caption": caption or None,
            "file_id": media["file_id"][:512],
            "mime_type": media.get("mime_type"),
            "file_size": media.get("file_size"),
            "url": None,
        }
    return None


def _resource_url(message: dict) -> str | None:
    text = message.get("text")
    if not isinstance(text, str):
        return None
    candidates = []
    for entity in message.get("entities", []):
        if not isinstance(entity, dict):
            continue
        if entity.get("type") == "text_link":
            candidates.append(entity.get("url"))
        elif entity.get("type") == "url":
            offset, length = entity.get("offset"), entity.get("length")
            if isinstance(offset, int) and isinstance(length, int):
                candidates.append(text[offset : offset + length])
    if not candidates:
        candidates = re.findall(r"https?://[^\s<>]+", text)
    return next(
        (
            url.rstrip(".,!?)]}")
            for url in candidates
            if isinstance(url, str) and url.startswith(("https://", "http://")) and len(url) <= 2048
        ),
        None,
    )


def extract_telegram_resource(message: dict) -> dict | None:
    """Extract a durable Telegram message reference for a shared file or URL."""
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    message_id = message.get("message_id")
    if chat.get("type") not in {"group", "supergroup"} or not chat_id:
        return None
    if not isinstance(message_id, int) or message_id <= 0:
        return None
    message_link = _resource_message_link(chat, chat_id, message_id)
    if not message_link:
        return None
    caption = message.get("caption") or message.get("text") or ""
    caption = caption[:2000] if isinstance(caption, str) else ""
    media = _extract_media_resource(message, message_link, message_id, caption)
    if media:
        return media
    url = _resource_url(message)
    if not url:
        return None
    return {
        "source_message_id": message_id,
        "message_link": message_link,
        "thread_id": message.get("message_thread_id"),
        "resource_type": "link",
        "title": (caption or url)[:255],
        "caption": caption or None,
        "file_id": None,
        "url": url,
    }


async def save_course_resource(database, chat_id: str, resource: dict, now_ms: int) -> bool:
    """Upsert one resource by source Telegram message for replay safety."""
    group = await database.prepare(
        "SELECT college_id FROM academic_groups WHERE chat_id = ? AND active = 1"
    ).bind(chat_id).first()
    if not group:
        return False
    await database.prepare(
        """
        INSERT INTO course_resources (
            college_id, chat_id, thread_id, source_message_id, message_link, resource_type,
            title, caption, file_id, url, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, source_message_id) DO UPDATE SET
            college_id = excluded.college_id,
            thread_id = excluded.thread_id,
            message_link = excluded.message_link,
            resource_type = excluded.resource_type,
            title = excluded.title,
            caption = excluded.caption,
            file_id = excluded.file_id,
            url = excluded.url
        """
    ).bind(
        group["college_id"],
        chat_id,
        resource["thread_id"],
        resource["source_message_id"],
        resource["message_link"],
        resource["resource_type"],
        resource["title"],
        resource["caption"],
        resource["file_id"],
        resource["url"],
        now_ms,
    ).run()
    return True


async def search_group_resources(database, chat_id: str, query: str, thread_id: int | None = None) -> str:
    """Find matching resources only in the invoking group and optional topic."""
    pattern = f"%{query.strip()[:100]}%"
    if thread_id is not None:
        result = await database.prepare(
            """
            SELECT title, caption, resource_type, message_link, thread_id
            FROM course_resources r
            JOIN academic_groups g ON g.chat_id = r.chat_id AND g.college_id = r.college_id AND g.active = 1
            WHERE r.chat_id = ? AND r.thread_id = ?
              AND (r.title LIKE ? OR r.caption LIKE ?)
            ORDER BY r.created_at_ms DESC
            LIMIT ?
            """
        ).bind(chat_id, thread_id, pattern, pattern, 10).all()
    else:
        result = await database.prepare(
            """
            SELECT title, caption, resource_type, message_link, thread_id
            FROM course_resources r
            JOIN academic_groups g ON g.chat_id = r.chat_id AND g.college_id = r.college_id AND g.active = 1
            WHERE r.chat_id = ? AND (r.title LIKE ? OR r.caption LIKE ?)
            ORDER BY r.created_at_ms DESC
            LIMIT ?
            """
        ).bind(chat_id, pattern, pattern, 10).all()
    resources = result["results"] or []
    if not resources:
        return "No matching course materials were found in this group."
    lines = ["<b>Course materials</b>", ""]
    for resource in resources:
        title = escape(resource["title"] or resource["resource_type"].title())
        link = escape(resource["message_link"], quote=True)
        lines.append(f'• <a href="{link}">{title}</a>')
        if resource.get("caption"):
            lines.append(f"  {escape(resource['caption'][:250])}")
    return "\n".join(lines)


async def dispatch_claimed_reminders(database, reminders, send_message, now_ms: int) -> None:
    """Deliver claimed rows and persist success or retry state."""
    for reminder in reminders:
        try:
            await send_message(reminder)
        except Exception:
            retry_ms = min(
                REMINDER_RETRY_BASE_MS * (2 ** (reminder["attempt_count"] - 1)),
                REMINDER_RETRY_MAX_MS,
            )
            retry_after_ms = now_ms + retry_ms
            await database.prepare(
                "UPDATE reminder_outbox SET status = 'failed', retry_after_ms = ?, lease_until_ms = NULL, last_error = ? WHERE id = ? AND status = 'sending'"
            ).bind(retry_after_ms, "telegram_send_failed", reminder["id"]).run()
        else:
            await database.prepare(
                "UPDATE reminder_outbox SET status = 'sent', sent_at_ms = ?, lease_until_ms = NULL, last_error = NULL WHERE id = ? AND status = 'sending'"
            ).bind(now_ms, reminder["id"]).run()


async def _send_personal_task_reminder(token: str, reminder: dict) -> None:
    try:
        await send_telegram_message(token, {
            **reminder,
            "message_text": escape(str(reminder.get("message_text") or "")),
        })
    except TelegramMessageRejected as exc:
        raise personal_task_service.DefiniteDeliveryError() from exc


_PART_MARKER_RESERVE = 40
_ATOMIC_HTML = re.compile(r"<(pre|blockquote)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HEADING_ONLY = re.compile(r"^<b>(?!.*<b>)[^\n]{1,140}</b>$")


def _split_html_by_characters(message: str, max_length: int) -> list[str]:
    """Last-resort splitter for a single oversized text line; keeps every chunk's tags balanced."""
    token_pattern = re.compile(
        r'<[^>]+>|&(?:#\d+|#x[0-9a-f]+|[a-z]+);|\s+|[^\s<>&]+|[<&>]',
        re.IGNORECASE,
    )
    chunks: list[str] = []
    current = ""
    open_tags: list[str] = []

    def closing_tags() -> str:
        return "".join(f"</{match.group(1)}>" for tag in reversed(open_tags)
                       if (match := re.match(r"<([a-zA-Z]+)", tag)))

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current + closing_tags())
        current = "".join(open_tags)

    for token in token_pattern.findall(str(message or "")):
        tag = re.fullmatch(r"<(/?)([a-zA-Z]+)(?:\s+[^>]*)?(/?)>", token)
        is_closing = bool(tag and tag.group(1))
        is_opening = bool(tag and not tag.group(1) and not tag.group(3)
                          and tag.group(2).lower() not in {"br"})
        if len(current) + len(token) + len(closing_tags()) > max_length and current.strip():
            flush()
        if len(current) + len(token) + len(closing_tags()) > max_length and not tag:
            for character in token:
                if len(current) + len(character) + len(closing_tags()) > max_length:
                    flush()
                current += character
            continue
        current += token
        if is_closing:
            name = tag.group(2).lower()
            for index in range(len(open_tags) - 1, -1, -1):
                opened = re.match(r"<([a-zA-Z]+)", open_tags[index])
                if opened and opened.group(1).lower() == name:
                    del open_tags[index:]
                    break
        elif is_opening:
            open_tags.append(token)
    if current.strip():
        chunks.append(current + closing_tags())
    return chunks


def _split_pre_block(block: str, max_length: int) -> list[str]:
    """Split an oversized <pre> at line boundaries, re-wrapping each piece so it stays valid code."""
    match = re.match(r"^(<pre>(?:<code[^>]*>)?)(.*?)((?:</code>)?</pre>)$", block, re.DOTALL | re.IGNORECASE)
    if not match:
        return _split_html_by_characters(block, max_length)
    opening, body, closing = match.groups()
    budget = max_length - len(opening) - len(closing)
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in body.split("\n"):
        while len(line) > budget:  # a single physical line longer than a message
            if current:
                pieces.append("\n".join(current))
                current, size = [], 0
            pieces.append(line[:budget])
            line = line[budget:]
        if current and size + len(line) + 1 > budget:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        pieces.append("\n".join(current))
    return [opening + piece + closing for piece in pieces if piece.strip()]


def _html_units(message: str, limit: int) -> list[tuple[str, str]]:
    """Break HTML into (text, separator-before) units, none longer than limit, never cutting a code block
    or paragraph unless that single block cannot fit in one message."""
    units: list[tuple[str, str]] = []
    protected: list[str] = []

    def hide(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    hidden = _ATOMIC_HTML.sub(hide, str(message or ""))

    def restore(text: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], text)

    for paragraph in re.split(r"\n{2,}", hidden):
        block = restore(paragraph).strip("\n")
        if not block.strip():
            continue
        if len(block) <= limit:
            units.append((block, "\n\n"))
            continue
        first = True
        for part in re.split(r"(\x00\d+\x00)", paragraph):
            if not part.strip():
                continue
            if re.fullmatch(r"\x00\d+\x00", part):
                atomic = protected[int(part.strip("\x00"))]
                # Leave headroom so the heading or lead-in that introduces a huge block can travel with it.
                piece_limit = limit - 160
                pieces = [atomic] if len(atomic) <= limit else (
                    _split_pre_block(atomic, piece_limit) if atomic.lower().startswith("<pre")
                    else _split_html_by_characters(atomic, piece_limit)
                )
                for piece in pieces:
                    units.append((piece, "\n\n" if first else "\n"))
                    first = False
                continue
            packed = ""
            for line in part.strip("\n").split("\n"):
                for fragment in ([line] if len(line) <= limit else _split_html_by_characters(line, limit)):
                    if packed and len(packed) + 1 + len(fragment) > limit:
                        units.append((packed, "\n\n" if first else "\n"))
                        first, packed = False, ""
                    packed = f"{packed}\n{fragment}" if packed else fragment
            if packed:
                units.append((packed, "\n\n" if first else "\n"))
                first = False
    return units


def split_telegram_html(message: str, max_length: int = 3_500) -> list[str]:
    """Split Telegram HTML at paragraph/section boundaries into complete, conservatively sized messages.

    Whole blocks (paragraphs, lists, code blocks, tables, callouts) are never cut; a heading or a lead-in
    ending in ':' always travels with the block it introduces; a new section starts a new message when the
    current one is already well filled. Only a single block larger than a message is split, at line
    boundaries (code) or word boundaries (prose), with tags re-opened so each part stays valid.
    """
    limit = max_length - _PART_MARKER_RESERVE
    units = _html_units(message, limit)
    merged: list[tuple[str, str]] = []
    index = 0
    while index < len(units):
        text, separator = units[index]
        while (
            index + 1 < len(units)
            and (_HEADING_ONLY.match(text) or text.rstrip().endswith(":"))
            and len(text) + 2 + len(units[index + 1][0]) <= limit
        ):
            text = f"{text}\n\n{units[index + 1][0]}"
            index += 1
        merged.append((text, separator))
        index += 1

    chunks: list[str] = []
    current = ""
    for text, separator in merged:
        starts_section = text.startswith(("<b>▎", "<b>📘"))
        joined = f"{current}{separator}{text}" if current else text
        if current and (len(joined) > limit or (starts_section and len(current) >= limit * 0.55)):
            chunks.append(current)
            current = text
        else:
            current = joined
    if current.strip():
        chunks.append(current)
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [chunks[0], *(
            f"<i>Part {number} of {total}</i>\n\n{chunk}" for number, chunk in enumerate(chunks[1:], 2)
        )]
    return chunks or [""]


def _telegram_plain_text(value: str) -> str:
    """Readable fallback when Telegram rejects HTML entities: drop tags, keep the words."""
    text = re.sub(r"<br\s*/?>", "\n", str(value or ""), flags=re.IGNORECASE)
    text = re.sub(r"</?(?:b|i|u|s|a|code|pre|blockquote|tg-spoiler)\b[^>]*>", "", text, flags=re.IGNORECASE)
    return html_unescape(text)


async def send_telegram_message(token: str, reminder: dict) -> int | None:
    """Send a Telegram message, continuing long HTML replies in complete parts."""
    from workers import fetch

    message_parts = split_telegram_html(reminder.get("message_text", ""))
    last_index = len(message_parts) - 1
    last_message_id = None
    for index, part in enumerate(message_parts):
        payload = {
            "chat_id": reminder["chat_id"],
            "text": part,
            "parse_mode": "HTML",
        }
        if reminder.get("thread_id") is not None:
            payload["message_thread_id"] = reminder["thread_id"]
        # The lesson button belongs under the final part, where the student finishes reading.
        if index == last_index and reminder.get("reply_markup") is not None:
            payload["reply_markup"] = reminder["reply_markup"]
        if reminder.get("disable_link_preview"):
            payload["link_preview_options"] = {"is_disabled": True}

        for attempt in range(2):
            response = await fetch(
                f"https://api.telegram.org/bot{token}/sendMessage",
                method="POST",
                headers={"content-type": "application/json"},
                body=json.dumps(payload),
            )
            body = await response.json()
            if response.status < 400 and body.get("ok"):
                result = body.get("result")
                if isinstance(result, dict) and type(result.get("message_id")) is int:
                    last_message_id = result["message_id"]
                break
            # A malformed entity must not abort a lesson whose earlier parts were already delivered
            # (a retry of the whole update would send them twice): resend this part as plain text.
            if attempt == 0 and "parse entities" in str(body.get("description", "")).lower():
                payload.pop("parse_mode", None)
                payload["text"] = _telegram_plain_text(part)
                continue
            raise TelegramMessageRejected("Telegram rejected message delivery")
    return last_message_id


async def clear_telegram_buttons(token: str, chat_id: str, message_id: int) -> None:
    """Remove an obsolete inline keyboard without editing the delivered message text."""
    from workers import fetch

    response = await fetch(
        f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
        method="POST",
        headers={"content-type": "application/json"},
        body=json.dumps({"chat_id": chat_id, "message_id": int(message_id), "reply_markup": {"inline_keyboard": []}}),
    )
    body = await response.json()
    if response.status >= 400 or not body.get("ok"):
        logger.info("telegram_keyboard_clear_failed chat_id=%s message_id=%s", chat_id, message_id)


async def send_telegram_message_draft(
    token: str,
    chat_id: str,
    draft_id: int,
    text: str,
    thread_id: int | None = None,
) -> None:
    """Update Telegram's ephemeral private-chat draft while a tutor answer streams."""
    from workers import fetch

    payload = {"chat_id": chat_id, "draft_id": int(draft_id), "text": str(text or "")}
    if text:
        payload["parse_mode"] = "HTML"
    if thread_id is not None:
        payload["message_thread_id"] = int(thread_id)
    response = await fetch(
        f"https://api.telegram.org/bot{token}/sendMessageDraft",
        method="POST",
        headers={"content-type": "application/json"},
        body=json.dumps(payload),
    )
    body = await response.json()
    if response.status >= 400 or not body.get("ok"):
        raise RuntimeError("Telegram rejected progressive draft")


class TelegramTutorDraft:
    """Throttle safe Telegram previews and let the final sendMessage persist the answer."""

    def __init__(self, token: str, chat_id: str, draft_id: int, thread_id: int | None = None):
        self.token = token
        self.chat_id = chat_id
        self.draft_id = draft_id or 1
        self.thread_id = thread_id
        self.active = False
        self.last_update = 0.0
        self.last_char_count = 0
        self.last_preview = ""
        self.pending_preview = ""
        self.flush_task = None

    async def start(self) -> bool:
        try:
            await send_telegram_message_draft(
                self.token, self.chat_id, self.draft_id, "", self.thread_id
            )
            self.active = True
        except Exception:
            logger.info("telegram_progressive_draft_unavailable")
        return self.active

    async def on_partial(self, markdown: str) -> None:
        if not self.active:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self.last_update and now - self.last_update < 0.75 and len(markdown) - self.last_char_count < 100:
            return
        live_text = re.sub(r"\[(?:M|W)\d+\]", "", markdown, flags=re.IGNORECASE)
        rendered = course_tutor_ai.markdown_to_telegram_html(live_text)
        if not rendered:
            return
        chunks = split_telegram_html(rendered, max_length=3_500)
        # Keep the opening context visible; the ephemeral preview must not replace part one with
        # whichever later part is currently being generated.
        preview = chunks[0] if len(chunks) == 1 else (
            chunks[0] + f"\n\n<i>writing the rest… ({len(chunks)} messages in total)</i>"
        )
        if preview == self.last_preview or preview == self.pending_preview:
            return
        self.pending_preview = preview
        self.last_update = now
        self.last_char_count = len(markdown)
        if self.flush_task is None or self.flush_task.done():
            self.flush_task = asyncio.create_task(self._flush_preview())

    async def _flush_preview(self) -> None:
        try:
            while self.active and self.pending_preview != self.last_preview:
                preview = self.pending_preview
                await send_telegram_message_draft(
                    self.token, self.chat_id, self.draft_id, preview, self.thread_id
                )
                self.last_preview = preview
        except Exception:
            self.active = False
            logger.info("telegram_progressive_draft_stopped")

    async def finish(self) -> None:
        """Wait for the newest preview before the persistent final message clears it."""
        if self.flush_task is not None:
            await self.flush_task


class ProgressiveTutorBinding:
    """Expose a private-chat draft callback alongside the normal Workers AI binding."""

    def __init__(self, binding, on_partial):
        self.binding = binding
        self.on_partial = on_partial

    async def run(self, model: str, payload: dict):
        return await self.binding.run(model, payload)


async def send_telegram_typing_action(token: str, chat_id: str, thread_id: int | None = None) -> None:
    """Show Telegram's native typing indicator while a tutor response is prepared."""
    from workers import fetch

    payload = {"chat_id": chat_id, "action": "typing"}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    try:
        await fetch(
            f"https://api.telegram.org/bot{token}/sendChatAction",
            method="POST",
            headers={"content-type": "application/json"},
            body=json.dumps(payload),
        )
    except Exception:
        # A progress indicator must never prevent the actual bot response.
        return


async def _keep_telegram_typing(token: str, chat_id: str, thread_id: int | None = None) -> None:
    """Refresh Telegram's short-lived typing indicator during slower tutor work."""
    while True:
        await asyncio.sleep(4)
        await send_telegram_typing_action(token, chat_id, thread_id)


async def answer_telegram_callback(token: str, callback_query_id: str, text: str = "") -> None:
    from workers import fetch

    response = await fetch(
        f"https://api.telegram.org/bot{token}/answerCallbackQuery",
        method="POST",
        headers={"content-type": "application/json"},
        body=json.dumps({"callback_query_id": callback_query_id, "text": text[:200]}),
    )
    body = await response.json()
    if response.status >= 400 or not body.get("ok"):
        raise RuntimeError("Telegram rejected callback acknowledgement")


async def send_telegram_poll(token: str, poll: dict) -> str:
    """Post one anonymous single-choice poll to a registered Telegram class group."""
    from workers import fetch

    payload = {
        "chat_id": poll["chat_id"],
        "question": poll["question"],
        "options": [
            option if isinstance(option, dict) else {"text": str(option)}
            for option in poll["options"]
        ],
        "is_anonymous": bool(poll.get("is_anonymous", True)),
        "type": poll.get("type", "regular"),
        "allows_multiple_answers": bool(poll.get("allows_multiple_answers", False)),
    }
    if poll.get("correct_option_id") is not None:
        payload["correct_option_ids"] = [poll["correct_option_id"]]
    if poll.get("thread_id") is not None:
        payload["message_thread_id"] = poll["thread_id"]
    response = await fetch(
        f"https://api.telegram.org/bot{token}/sendPoll",
        method="POST",
        headers={"content-type": "application/json"},
        body=json.dumps(payload),
    )
    body = await response.json()
    if response.status >= 400 or not body.get("ok"):
        raise RuntimeError("Telegram rejected class poll")
    return str(body["result"]["poll"]["id"])


async def send_class_poll_once(database, token: str, update_id: int, poll: dict, now_ms: int) -> bool:
    """Make one durable send attempt for this Telegram update id."""
    claimed = await poll_service.claim_class_poll(database, update_id, poll["chat_id"], now_ms)
    if not claimed:
        return False
    try:
        poll_id = await send_telegram_poll(token, poll)
    except Exception:
        await poll_service.finish_class_poll(
            database, update_id, "failed", now_ms, error="telegram_poll_send_failed"
        )
        raise
    await poll_service.finish_class_poll(database, update_id, "sent", now_ms, poll_id=poll_id)
    return True


def _quiz_question_message(quiz: dict, question: dict, attempt_id: int) -> dict:
    options = question["options"]
    return {
        "chat_id": None,
        "message_text": f"<b>{escape(quiz['title'])}</b> — {question['position']}\n\n{escape(question['prompt'])}",
        "reply_markup": {
            "inline_keyboard": [[{
                "text": option[:64],
                "callback_data": f"quiz:{attempt_id}:{question['id']}:{index}",
            }] for index, option in enumerate(options)]
        },
    }


def _format_academic_reminder(item: AcademicItemSync) -> str:
    """Render reminder content from academic fields, escaping Telegram HTML."""
    title = escape(item.title or item.item_type.title())
    lines = [f"<b>{title} — Reminder</b>", ""]
    if item.course_name:
        lines.extend(["<code>Course</code>", escape(item.course_name), ""])
    if item.deadline_ms is not None:
        from datetime import datetime, timedelta, timezone

        # Addis Ababa is UTC+03:00 year-round; avoid tzdata, which Pyodide may not bundle.
        deadline = datetime.fromtimestamp(item.deadline_ms / 1000, tz=timezone.utc) + timedelta(hours=3)
        lines.extend(
            [
                "<code>Deadline</code>",
                escape(deadline.strftime("%B %d, %Y — %I:%M %p")) + " (Addis Ababa Time)",
                "",
            ]
        )
    if item.room:
        lines.extend(["<code>Room</code>", escape(item.room), ""])
    if item.coverage:
        lines.extend(["<code>Coverage</code>", escape(item.coverage), ""])
    return "\n".join(lines)


async def sync_academic_items(database, items: list[AcademicItemSync]) -> int:
    """Idempotently mirror academic records and schedules into the D1 queue."""
    synced = 0
    for item in items:
        group = await database.prepare(
            "SELECT college_id FROM academic_groups WHERE chat_id = ? AND active = 1"
        ).bind(item.chat_id).first()
        if not group:
            continue
        topic_course = await mapped_topic_course(database, item.chat_id, item.thread_id)
        course_name = item.course_name or (topic_course["course_name"] if topic_course else None)
        course_id = await resolve_course_id(database, item.chat_id, topic_course, item.course_name)
        await database.prepare(
            """
            INSERT INTO academic_items (
                source_item_id, group_id, chat_id, thread_id, item_type, title,
                deadline_ms, room, coverage, item_status, updated_at_ms, course_name,
                confidence, college_id, course_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_item_id) DO UPDATE SET
                group_id = excluded.group_id,
                chat_id = excluded.chat_id,
                thread_id = excluded.thread_id,
                item_type = excluded.item_type,
                title = excluded.title,
                deadline_ms = excluded.deadline_ms,
                room = excluded.room,
                coverage = excluded.coverage,
                item_status = excluded.item_status,
                updated_at_ms = excluded.updated_at_ms,
                course_name = excluded.course_name,
                confidence = excluded.confidence,
                college_id = excluded.college_id,
                course_id = excluded.course_id
            """
        ).bind(
            item.source_item_id,
            item.group_id,
            item.chat_id,
            item.thread_id,
            item.item_type,
            item.title,
            item.deadline_ms,
            item.room,
            item.coverage,
            item.status,
            int(__import__("time").time() * 1000),
            course_name,
            item.confidence,
            group["college_id"],
            course_id,
        ).run()

        message_text = _format_academic_reminder(item)
        for reminder in item.reminders:
            initial_status = (
                "sent" if reminder.sent else "cancelled" if reminder.cancelled else "pending"
            )
            await database.prepare(
                """
                INSERT INTO reminder_outbox (
                    source_item_id, source_reminder_id, group_id, chat_id, thread_id,
                    send_time_ms, message_text, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_item_id, source_reminder_id) DO UPDATE SET
                    group_id = excluded.group_id,
                    chat_id = excluded.chat_id,
                    thread_id = excluded.thread_id,
                    send_time_ms = excluded.send_time_ms,
                    message_text = excluded.message_text,
                    status = CASE
                        WHEN reminder_outbox.status IN ('sent', 'sending', 'failed')
                            THEN reminder_outbox.status
                        ELSE excluded.status
                    END
                """
            ).bind(
                item.source_item_id,
                reminder.source_reminder_id,
                item.group_id,
                item.chat_id,
                item.thread_id,
                reminder.send_time_ms,
                message_text,
                initial_status,
            ).run()
        synced += 1
    return synced


async def claim_due_reminders(database, now_ms: int) -> list[dict]:
    """Atomically lease due reminders and recover expired leases."""
    result = await database.prepare(
        """
        UPDATE reminder_outbox
        SET status = 'sending',
            attempt_count = attempt_count + 1,
            lease_until_ms = ?,
            retry_after_ms = NULL
        WHERE id IN (
            SELECT id
            FROM reminder_outbox
            WHERE send_time_ms <= ?
              AND attempt_count < ?
              AND (
                  status = 'pending'
                  OR (status = 'failed' AND retry_after_ms <= ?)
                  OR (status = 'sending' AND lease_until_ms <= ?)
              )
            ORDER BY send_time_ms ASC
            LIMIT ?
        )
          AND (
              status = 'pending'
              OR (status = 'failed' AND retry_after_ms <= ?)
              OR (status = 'sending' AND lease_until_ms <= ?)
          )
        RETURNING id, chat_id, thread_id, message_text, attempt_count
        """
    ).bind(
        now_ms + REMINDER_LEASE_MS,
        now_ms,
        REMINDER_MAX_ATTEMPTS,
        now_ms,
        now_ms,
        REMINDER_BATCH_SIZE,
        now_ms,
        now_ms,
    ).run()
    return result["results"] or []


async def claim_telegram_update(database, update_id: int, now_ms: int) -> bool:
    """Lease an update once; failed or expired work can be retried by Telegram."""
    result = await database.prepare(
        """
        INSERT INTO telegram_update_receipts (
            update_id, status, attempt_count, lease_until_ms, received_at_ms
        ) VALUES (?, 'processing', 1, ?, ?)
        ON CONFLICT(update_id) DO UPDATE SET
            status = 'processing',
            attempt_count = telegram_update_receipts.attempt_count + 1,
            lease_until_ms = excluded.lease_until_ms,
            last_error = NULL
        WHERE telegram_update_receipts.status = 'failed'
           OR (telegram_update_receipts.status = 'processing'
               AND telegram_update_receipts.lease_until_ms <= ?)
        RETURNING update_id
        """
    ).bind(update_id, now_ms + REMINDER_LEASE_MS, now_ms, now_ms).run()
    return bool(result["results"])


async def finish_telegram_update(database, update_id: int, status: str, now_ms: int, error: str | None = None) -> None:
    """Persist the terminal processing state without storing message contents."""
    await database.prepare(
        "UPDATE telegram_update_receipts SET status = ?, lease_until_ms = NULL, completed_at_ms = ?, last_error = ? WHERE update_id = ?"
    ).bind(status, now_ms, error, update_id).run()


async def is_telegram_chat_admin(token: str, chat_id: str, user_id: int) -> bool:
    """Ask Telegram for current group admin status before accepting official items."""
    from workers import fetch

    response = await fetch(
        f"https://api.telegram.org/bot{token}/getChatMember",
        method="POST",
        headers={"content-type": "application/json"},
        body=json.dumps({"chat_id": chat_id, "user_id": user_id}),
    )
    body = await response.json()
    if response.status >= 400 or not body.get("ok"):
        raise RuntimeError("Telegram admin check failed")
    return body["result"].get("status") in {"creator", "administrator"}


async def is_telegram_chat_member(token: str, chat_id: str, user_id: int) -> bool:
    from workers import fetch

    response = await fetch(
        f"https://api.telegram.org/bot{token}/getChatMember",
        method="POST",
        headers={"content-type": "application/json"},
        body=json.dumps({"chat_id": chat_id, "user_id": user_id}),
    )
    body = await response.json()
    if response.status >= 400 or not body.get("ok"):
        raise RuntimeError("Telegram membership check failed")
    member = body["result"]
    status = member.get("status")
    return status in {"creator", "administrator", "member"} or (
        status == "restricted" and member.get("is_member", False)
    )


def _invite_code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


async def register_group_with_invite(
    database,
    token: str,
    user_id: int,
    chat_id: str,
    invite_code: str,
    now_ms: int,
) -> bool:
    """Verify Telegram group-admin status and consume one college invite use."""
    if not await is_telegram_chat_admin(token, chat_id, user_id):
        return False
    invite = await database.prepare(
        """
        UPDATE college_invite_codes
        SET remaining_uses = remaining_uses - 1
        WHERE code_hash = ? AND active = 1 AND remaining_uses > 0
          AND (expires_at_ms IS NULL OR expires_at_ms > ?)
        RETURNING college_id
        """
    ).bind(_invite_code_hash(invite_code), now_ms).first()
    if not invite:
        return False
    await database.prepare(
        """
        INSERT INTO academic_groups (chat_id, college_id, group_title, active, created_by_user_id, created_at_ms)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            college_id = excluded.college_id,
            group_title = excluded.group_title,
            active = 1,
            created_by_user_id = excluded.created_by_user_id
        """
    ).bind(chat_id, invite["college_id"], None, user_id, now_ms).run()
    return True


async def is_group_admin_or_representative(
    database,
    token: str,
    chat_id: str,
    user_id: int,
) -> bool:
    """Allow registered Telegram admins or explicitly assigned class reps."""
    group = await database.prepare(
        "SELECT chat_id FROM academic_groups WHERE chat_id = ? AND active = 1"
    ).bind(chat_id).first()
    if not group:
        return False
    if await is_telegram_chat_admin(token, chat_id, user_id):
        return True
    representative = await database.prepare(
        "SELECT user_id FROM group_representatives WHERE chat_id = ? AND user_id = ? AND active = 1"
    ).bind(chat_id, user_id).first()
    return bool(representative)


async def add_group_representative(
    database,
    chat_id: str,
    user_id: int,
    added_by_user_id: int,
    now_ms: int,
) -> bool:
    """Assign a class rep to a registered group; only caller checks Telegram admin."""
    result = await database.prepare(
        """
        INSERT INTO group_representatives (chat_id, user_id, added_by_user_id, active, created_at_ms)
        SELECT chat_id, ?, ?, 1, ? FROM academic_groups WHERE chat_id = ? AND active = 1
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            added_by_user_id = excluded.added_by_user_id,
            active = 1,
            created_at_ms = excluded.created_at_ms
        RETURNING user_id
        """
    ).bind(user_id, added_by_user_id, now_ms, chat_id).first()
    return bool(result)


async def remove_group_representative(database, chat_id: str, user_id: int) -> bool:
    result = await database.prepare(
        "UPDATE group_representatives SET active = 0 WHERE chat_id = ? AND user_id = ? AND active = 1 RETURNING user_id"
    ).bind(chat_id, user_id).first()
    return bool(result)


async def map_course_to_topic(
    database,
    token: str,
    chat_id: str,
    thread_id: int,
    course_name: str,
    user_id: int,
    now_ms: int,
) -> bool:
    """Map one registered forum topic to a college-scoped course."""
    if not await is_group_admin_or_representative(database, token, chat_id, user_id):
        return False
    group = await database.prepare(
        "SELECT college_id, current_semester FROM academic_groups WHERE chat_id = ? AND active = 1"
    ).bind(chat_id).first()
    if not group:
        return False
    normalized = normalize_course_name(course_name)
    course = await database.prepare(
        """
        INSERT INTO courses (college_id, chat_id, course_name, normalized_name, active, created_at_ms)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(chat_id, normalized_name) DO UPDATE SET
            course_name = excluded.course_name,
            active = 1
        RETURNING id
        """
    ).bind(group["college_id"], chat_id, course_name.strip(), normalized, now_ms).first()
    if not course:
        return False
    await database.prepare(
        """
        INSERT INTO group_topics (chat_id, thread_id, course_id, created_by_user_id, created_at_ms)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, thread_id) DO UPDATE SET
            course_id = excluded.course_id,
            created_by_user_id = excluded.created_by_user_id,
            created_at_ms = excluded.created_at_ms
        """
    ).bind(chat_id, thread_id, course["id"], user_id, now_ms).run()
    if group.get("current_semester"):
        await course_quiz_service.activate_course_offering(
            database, chat_id, int(course["id"]), group["college_id"], group["current_semester"], now_ms
        )
    return True


async def mapped_topic_course(database, chat_id: str, thread_id: int | None) -> dict | None:
    if thread_id is None:
        return None
    result = await database.prepare(
        """
        SELECT c.id AS course_id, c.course_name, c.college_id FROM group_topics t
        JOIN courses c ON c.id = t.course_id
        JOIN academic_groups g ON g.chat_id = t.chat_id AND g.college_id = c.college_id
        WHERE t.chat_id = ? AND t.thread_id = ? AND g.active = 1 AND c.active = 1
        """
    ).bind(chat_id, thread_id).first()
    return result


async def resolve_course_id(database, chat_id: str, topic_course: dict | None, course_name: str | None) -> int | None:
    if topic_course and not course_name:
        return int(topic_course["course_id"])
    if not course_name:
        return None
    row = await database.prepare(
        "SELECT id FROM courses WHERE chat_id = ? AND normalized_name = ? AND active = 1"
    ).bind(chat_id, normalize_course_name(course_name)).first()
    return int(row["id"]) if row and row.get("id") is not None else None


def _addis_datetime_text(timestamp_ms: int) -> str:
    local_time = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc) + ADDIS_OFFSET
    return local_time.strftime("%B %d, %Y — %I:%M %p")


async def _save_telegram_academic_item(database, *, update_id: int, message: dict, chat_id: str, item: dict, now_ms: int) -> bool:
    """Persist a class-rep item and its future reminders idempotently in D1."""
    group = await database.prepare(
        "SELECT college_id FROM academic_groups WHERE chat_id = ? AND active = 1"
    ).bind(chat_id).first()
    if not group:
        return False
    source_item_id = 9_000_000_000_000_000 + update_id
    group_id = int(chat_id)
    thread_id = message.get("message_thread_id")
    source_message_id = message.get("message_id")
    link = None
    if chat_id.startswith("-100") and source_message_id:
        link = f"https://t.me/c/{chat_id[4:]}/{source_message_id}"
    topic_course = await mapped_topic_course(database, chat_id, thread_id)
    course_name = item.get("course_name") or (topic_course["course_name"] if topic_course else None)
    course_id = await resolve_course_id(database, chat_id, topic_course, item.get("course_name"))
    event = AcademicItemSync(
        source_item_id=source_item_id,
        group_id=group_id,
        chat_id=chat_id,
        thread_id=thread_id,
        item_type=item["item_type"],
        title=item["title"],
        course_name=course_name,
        deadline_ms=item["deadline_ms"],
        room=item.get("room"),
        coverage=item.get("coverage"),
        confidence=item.get("confidence"),
        status=item.get("status", "active"),
    )
    reminders = []
    if event.deadline_ms is not None and event.status == "active":
        reminders = build_reminder_schedule(source_item_id, event.deadline_ms, now_ms)
    await database.prepare(
        """
        INSERT INTO academic_items (
            source_item_id, group_id, chat_id, thread_id, item_type, title,
            deadline_ms, item_status, updated_at_ms, source_update_id,
            source_message_id, source_message_link, course_name, room, coverage, confidence,
            college_id, course_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_item_id) DO UPDATE SET
            chat_id = excluded.chat_id,
            thread_id = excluded.thread_id,
            item_type = excluded.item_type,
            title = excluded.title,
            deadline_ms = excluded.deadline_ms,
            item_status = excluded.item_status,
            updated_at_ms = excluded.updated_at_ms,
            source_message_id = excluded.source_message_id,
            source_message_link = excluded.source_message_link,
            course_name = excluded.course_name,
            room = excluded.room,
            coverage = excluded.coverage,
            confidence = excluded.confidence,
            college_id = excluded.college_id,
            course_id = excluded.course_id
        """
    ).bind(
        source_item_id,
        group_id,
        chat_id,
        thread_id,
        item["item_type"],
        item["title"],
        item["deadline_ms"],
        event.status,
        now_ms,
        update_id,
        source_message_id,
        link,
        course_name,
        item.get("room"),
        item.get("coverage"),
        item.get("confidence"),
        group["college_id"],
        course_id,
    ).run()

    message_text = _format_academic_reminder(event)
    for reminder in reminders:
        await database.prepare(
            """
            INSERT INTO reminder_outbox (
                source_item_id, source_reminder_id, group_id, chat_id, thread_id,
                send_time_ms, message_text, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(source_item_id, source_reminder_id) DO UPDATE SET
                send_time_ms = excluded.send_time_ms,
                message_text = excluded.message_text
            """
        ).bind(
            source_item_id,
            reminder["source_reminder_id"],
            group_id,
            chat_id,
            thread_id,
            reminder["send_time_ms"],
            message_text,
        ).run()
    return True


async def _approve_ai_item(database, chat_id: str, source_update_id: int, now_ms: int) -> tuple[str, int]:
    """Confirm a pending AI suggestion and create its reminder schedule."""
    item = await database.prepare(
        """
        SELECT source_item_id, group_id, chat_id, thread_id, item_type, title,
               deadline_ms, room, coverage, course_name, confidence, item_status
        FROM academic_items
        WHERE source_update_id = ? AND chat_id = ?
        LIMIT 1
        """
    ).bind(source_update_id, chat_id).first()
    if not item:
        return "I couldn't find that event in this group.", 0
    if item["item_status"] != "new":
        return "That event is already confirmed.", 0

    await database.prepare(
        "UPDATE academic_items SET item_status = 'active', updated_at_ms = ? WHERE source_item_id = ? AND chat_id = ?"
    ).bind(now_ms, item["source_item_id"], chat_id).run()
    reminders = []
    if item["deadline_ms"] is not None:
        reminders = build_reminder_schedule(item["source_item_id"], item["deadline_ms"], now_ms)
        sync_item = AcademicItemSync(
            source_item_id=item["source_item_id"],
            group_id=item["group_id"],
            chat_id=item["chat_id"],
            thread_id=item["thread_id"],
            item_type=item["item_type"],
            title=item["title"],
            course_name=item["course_name"],
            deadline_ms=item["deadline_ms"],
            room=item["room"],
            coverage=item["coverage"],
            confidence=item["confidence"],
            status="active",
        )
        message_text = _format_academic_reminder(sync_item)
        for reminder in reminders:
            await database.prepare(
                """
                INSERT INTO reminder_outbox (
                    source_item_id, source_reminder_id, group_id, chat_id, thread_id,
                    send_time_ms, message_text, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(source_item_id, source_reminder_id) DO UPDATE SET
                    send_time_ms = excluded.send_time_ms,
                    message_text = excluded.message_text
                """
            ).bind(
                sync_item.source_item_id,
                reminder["source_reminder_id"],
                sync_item.group_id,
                sync_item.chat_id,
                sync_item.thread_id,
                reminder["send_time_ms"],
                message_text,
            ).run()
    return f"✅ Confirmed: {escape(item['title'] or item['item_type'].title())}.", len(reminders)


def _welcome_message(chat_type: str = "private") -> str:
    if chat_type in {"group", "supergroup"}:
        return (
            "<b>Welcome to Academic Assistant</b>\n"
            "Your class group’s place for deadlines, course materials, and study help.\n\n"
            "<b>Use a button below or the Menu button</b> beside the message box to choose an action.\n"
            "• <code>/upcoming</code> — see recorded assignments and exams.\n"
            "• <code>/ask</code> — get a clear study explanation; in a mapped course topic it uses course materials and sources.\n"
            "• <code>/resources</code> — find shared files and links.\n\n"
            "Class reps can post deadlines and polls from the same menu."
        )
    return (
        "<b>Welcome to Academic Assistant</b>\n"
        "Keep up with classwork, find course materials, and get clear study explanations.\n\n"
        "<b>Plan</b>\n"
        "Save private tasks and one-time reminders here; tap <b>My tasks</b> to manage them.\n\n"
        "<b>Study</b>\n"
        "Ask me anything here. In a mapped course topic, <code>/ask</code> also checks that course’s materials and live sources.\n\n"
        "<b>Make the tutor yours</b>\n"
        "Tap <b>My study preferences</b> below to view or update how I tutor you, or erase saved tutor memory.\n\n"
        "<b>Get started</b>\n"
        "Tap a button below, or use the <b>Menu</b> button next to the message box."
    )


def _welcome_reply_markup(chat_type: str) -> dict:
    if chat_type in {"group", "supergroup"}:
        rows = [
            [
                {"text": "📅 Upcoming deadlines", "callback_data": "home:upcoming"},
                {"text": "📚 Ask in this topic", "callback_data": "home:ask"},
            ],
            [
                {"text": "🔎 Find class materials", "callback_data": "home:resources"},
                {"text": "❔ Help", "callback_data": "home:help"},
            ],
        ]
    else:
        rows = [
            [
                {"text": "📚 Ask the tutor", "callback_data": "home:ask"},
                {"text": "🖼 Analyze a file or image", "callback_data": "home:upload"},
            ],
            [
                {"text": "🗂 My tasks", "callback_data": "home:tasks"},
                {"text": "⏰ Add reminder", "callback_data": "home:add_reminder"},
            ],
            [
                {"text": "🧠 My study preferences", "callback_data": "home:memory"},
                {"text": "❔ Help", "callback_data": "home:help"},
            ],
        ]
    return {"inline_keyboard": rows}


HOME_ASK_PROMPT = "Reply to this message with your study question. I’ll answer here or use this course topic."
HOME_RESOURCE_PROMPT = "Reply with a keyword to find matching files and links in this class group."
HOME_REMEMBER_PROMPT = "Reply with the study style you want me to remember, for example: use simple explanations and worked examples."
HOME_TASK_PROMPT = "Reply with a task or reminder, for example: Remind me to submit the lab tomorrow at 5 PM."
HOME_TIMEZONE_PROMPT = "Reply with an IANA timezone, for example: Africa/Addis_Ababa or Europe/London."
HOME_TASK_CLARIFICATION_PROMPT = "Reply to the reminder clarification question."
HOME_RESCHEDULE_PROMPT = "Reply with the new reminder date and time."
HOME_RENAME_PROMPT = "Reply with the new task title."
HOME_SETUP_SEMESTER_PROMPT = "Reply with the active semester label, for example: Fall 2026."


def _help_message(chat_type: str = "private") -> str:
    common = (
        "<b>What I can do</b>\n"
        "• <b>Study tutor:</b> ask a question here, or use <code>/ask</code> in a mapped course topic.\n"
        "• <b>Personal study style:</b> <code>/remember</code> saves a preference; <code>/memory</code> shows it privately; <code>/forget_me</code> erases your tutor history and preference.\n"
    )
    if chat_type not in {"group", "supergroup"}:
        return (
            "<b>Academic Assistant help</b>\n\n" + common +
            "• <b>Personal assistant:</b> tell me to remind you about something, or open <b>My tasks</b> to add and manage tasks. Use <code>/timezone</code> to set your local timezone.\n"
            "Open the <b>Menu</b> beside the message box to pick a command. In your class group, use its menu for deadlines and shared materials.\n\n"
            "<b>Connect a class group</b>\n"
            "A group admin can run <code>/group_id</code> in the group, then send <code>/group_setup -100CHATID INVITE_CODE</code> to me privately."
        )
    return (
        "<b>Academic Assistant help</b>\n\n" + common +
        "• <b>Deadlines:</b> <code>/upcoming</code>\n"
        "• <b>Find shared files:</b> <code>/resources keyword</code>\n"
        "• <b>Class rep:</b> <code>/assignment Title | YYYY-MM-DD HH:MM</code>, <code>/exam Title | YYYY-MM-DD HH:MM</code>, or <code>/class_poll Question | Option A | Option B</code>.\n\n"
        "<b>Group admin setup</b>\n"
        "Use <code>/semester_set Fall 2026</code>, map each course topic with <code>/course_map Course Name</code>, and run <code>/quiz_topic</code> in the Quiz topic. Use <code>/quiz_status</code> to check setup.\n\n"
        "Tap the <b>Menu</b> button for available actions. Times use Addis Ababa time."
    )


async def ensure_telegram_command_menu(token: str) -> None:
    """Register scoped commands so Telegram's menu button lists useful actions."""
    global BOT_COMMAND_MENU_READY
    if BOT_COMMAND_MENU_READY or not token:
        return

    command_scopes = [
        ({"type": "all_private_chats"}, [
            ("start", "Open the assistant"),
            ("help", "How to use the assistant"),
            ("ask", "Ask a study question"),
            ("remember", "Save a study preference"),
            ("memory", "View your preference privately"),
            ("forget_me", "Erase your tutor memory"),
            ("tasks", "List your personal tasks"),
            ("timezone", "Set your reminder timezone"),
            ("forget_tasks", "Delete all your personal tasks"),
            ("group_setup", "Connect a class group using an invite code"),
        ]),
        ({"type": "all_group_chats"}, [
            ("help", "Show student and class help"),
            ("ask", "Ask in this course topic"),
            ("upcoming", "See assignments and exams"),
            ("resources", "Find shared files and links"),
            ("digest_on", "Get the weekly private digest"),
            ("digest_off", "Stop the weekly digest"),
            ("assignment", "Add an assignment deadline"),
            ("exam", "Add an exam deadline"),
            ("class_poll", "Post an anonymous class poll"),
            ("approve", "Confirm a suggested deadline"),
            ("group_id", "Get this group’s ID"),
            ("semester_set", "Set the active semester"),
            ("course_map", "Map this topic to a course"),
            ("quiz_topic", "Set the Quiz topic"),
            ("quiz_source", "Add course material for quizzes"),
            ("quiz_status", "Check quiz setup and status"),
            ("rep_add", "Assign a class representative"),
            ("rep_remove", "Remove a class representative"),
            ("group_setup", "Review and map this semester’s course topics"),
        ]),
    ]
    try:
        from workers import fetch

        for scope, command_rows in command_scopes:
            response = await fetch(
                f"https://api.telegram.org/bot{token}/setMyCommands",
                method="POST",
                headers={"content-type": "application/json"},
                body=json.dumps({
                    "scope": scope,
                    "commands": [
                        {"command": command, "description": description}
                        for command, description in command_rows
                    ],
                }),
            )
            body = await response.json()
            if response.status >= 400 or not body.get("ok"):
                raise RuntimeError("Telegram rejected command menu configuration")
        BOT_COMMAND_MENU_READY = True
    except Exception:
        logger.info("Telegram command menu setup will retry on the next /start")


async def _upcoming_message(database, chat_id: str, now_ms: int) -> str:
    rows = await database.prepare(
        """
        SELECT i.item_type, i.title, i.deadline_ms, i.item_status
        FROM academic_items i
        JOIN academic_groups g ON g.chat_id = i.chat_id AND g.college_id = i.college_id
        WHERE i.chat_id = ? AND g.active = 1
          AND i.item_status IN ('new', 'verified', 'active') AND i.deadline_ms >= ?
        ORDER BY i.deadline_ms ASC
        LIMIT 10
        """
    ).bind(chat_id, now_ms).all()
    upcoming = rows["results"] or []
    if not upcoming:
        return "No upcoming assignments or exams are recorded in this group."
    lines = ["<b>Upcoming assignments and exams</b>", ""]
    for row in upcoming:
        label = escape(row["item_type"].title())
        title = escape(row["title"] or label)
        lines.append(f"• <b>{label}:</b> {title}")
        if row["item_status"] == "new":
            lines.append("  <i>Needs class admin review</i>")
        lines.append(f"  {_addis_datetime_text(row['deadline_ms'])} (Addis Ababa Time)")
    return "\n".join(lines)


async def _approve_command(database, token: str, message: dict, chat: dict, chat_id: str, text: str, now_ms: int) -> str | None:
    approval = re.fullmatch(r"\s*/approve(?:@\w+)?\s+(\d+)\s*", text, re.IGNORECASE)
    if not approval:
        return None
    if chat.get("type") not in {"group", "supergroup"}:
        return "Approve suggestions in the class group they came from."
    user_id = (message.get("from") or {}).get("id")
    if not user_id or not await is_group_admin_or_representative(database, token, chat_id, int(user_id)):
        return "Only a registered class group admin or representative can confirm an academic suggestion."
    response, _ = await _approve_ai_item(database, chat_id, int(approval.group(1)), now_ms)
    return response


async def _save_and_announce_ai_event(database, message: dict, chat_id: str, update_id: int, event: dict, now_ms: int) -> str:
    saved = await _save_telegram_academic_item(
        database,
        update_id=update_id,
        message=message,
        chat_id=chat_id,
        item=event,
        now_ms=now_ms,
    )
    if not saved:
        return "This group is not connected to a college yet. A group admin can link it privately with <code>/group_setup CHAT_ID INVITE_CODE</code>."
    label = escape(event["item_type"].title())
    title = escape(event["title"])
    course = f"\nCourse: {escape(event['course_name'])}" if event.get("course_name") else ""
    deadline = (
        f"\nDeadline: {escape(_addis_datetime_text(event['deadline_ms']))} (Addis Ababa Time)"
        if event.get("deadline_ms") is not None
        else ""
    )
    if event["status"] == "new":
        return (
            f"🔎 <b>Possible {label.lower()} detected — needs review</b>\n"
            f"{title}{course}{deadline}\nConfirm with <code>/approve {update_id}</code>."
        )
    return f"🤖 <b>{label} detected</b>\n{title}{course}{deadline}\nPlease verify the details."


async def _explicit_event_command(database, token: str, message: dict, chat: dict, chat_id: str, event: dict, update_id: int, now_ms: int) -> str:
    if chat.get("type") not in {"group", "supergroup"}:
        return "Add assignments and exams in the class group so reminders go to the right place."
    user_id = (message.get("from") or {}).get("id")
    if not user_id or not await is_group_admin_or_representative(database, token, chat_id, int(user_id)):
        return "Only a registered class group admin or representative can add official assignment and exam dates."
    saved = await _save_telegram_academic_item(
        database,
        update_id=update_id,
        message=message,
        chat_id=chat_id,
        item=event,
        now_ms=now_ms,
    )
    if not saved:
        return "This group is not connected to a college yet."
    return (
        f"✅ <b>{escape(event['item_type'].title())} recorded</b>\n"
        f"{escape(event['title'])}\n"
        f"Deadline: {escape(_addis_datetime_text(event['deadline_ms']))} (Addis Ababa Time)"
    )


async def _group_setup_command(database, token: str, message: dict, chat: dict, text: str, now_ms: int) -> str | None:
    if not re.match(r"^\s*/group_setup(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return None
    if chat.get("type") != "private":
        return "Send the group setup command privately so the invite code does not appear in group history."
    parsed = parse_group_setup_command(text)
    if not parsed:
        return "Use <code>/group_setup -100CHATID COLLEGE_INVITE_CODE</code> in a private chat with the bot."
    user_id = (message.get("from") or {}).get("id")
    if not user_id:
        return "I couldn't identify your Telegram account."
    registered = await register_group_with_invite(
        database,
        token,
        int(user_id),
        parsed["chat_id"],
        parsed["invite_code"],
        now_ms,
    )
    if not registered:
        return "Group setup failed. Confirm you are an admin of that group and the college invite code is valid."
    return "✅ Class group linked to its college. Admins can now map forum topics with <code>/course_map Course Name</code>."


def _setup_preview_reply(state: dict) -> TelegramReply:
    decisions = state.get("decisions") or []
    exact = [item for item in decisions if item.get("status") == "auto_apply"]
    uncertain = [item for item in decisions if item.get("status") == "needs_admin_choice"]
    already = sum(item.get("status") == "already_mapped" for item in decisions)
    lines = [
        f"<b>🧭 Course setup · {escape(str(state.get('semester_label') or 'Current semester'))}</b>",
        f"Group: {escape(str(state.get('group_title') or 'Class group'))}",
        "",
        f"Topics found: <b>{len(decisions)}</b> · clear matches: <b>{len(exact)}</b> · already mapped: <b>{already}</b> · need your choice: <b>{len(uncertain)}</b>",
    ]
    unmapped_offerings = state.get("unmapped_offerings") or []
    if unmapped_offerings:
        lines.append(f"\n<b>Courses without a topic mapping ({len(unmapped_offerings)})</b>")
        lines.extend(f"• {escape(str(item['course_name']))}" for item in unmapped_offerings[:10])
        if len(unmapped_offerings) > 10:
            lines.append(f"• And {len(unmapped_offerings) - 10} more")
    keyboard = []
    if exact:
        lines.append("\n<b>Ready to map</b>")
        lines.extend(f"• {escape(item['topic_title'])} → {escape(item['course_name'])}" for item in exact[:12])
        if len(exact) > 12:
            lines.append(f"• And {len(exact) - 12} more exact matches")
        keyboard.append([{"text": f"✅ Apply {len(exact)} clear matches", "callback_data": f"setup:apply:{int(state['run_id'])}"}])
    if uncertain:
        lines.append("\n<b>Please choose a course for these topics</b>")
        for item in uncertain[:8]:
            lines.append(f"• <b>{escape(item['topic_title'])}</b>")
            for candidate in item.get("candidates", [])[:5]:
                keyboard.append([{
                    "text": f"{item['topic_title'][:22]} → {candidate['course_name'][:28]}",
                    "callback_data": f"setup:choose:{int(state['run_id'])}:{int(item['thread_id'])}:{int(candidate['course_id'])}",
                }])
        if len(uncertain) > 8:
            lines.append(f"{len(uncertain) - 8} more topics can be mapped manually inside their topic with /course_map.")
    if not decisions:
        lines.append("\nI haven’t observed any forum topics yet. Map existing topics from inside each one with <code>/course_map Course Name</code>. I’ll record new or renamed topics when Telegram sends those service events.")
    elif not uncertain:
        lines.append("\nUncertain matches are left untouched. You can map one inside its topic with /course_map Course Name.")
    keyboard.append([{"text": "↩️ Undo latest setup", "callback_data": f"setup:undo:{int(state['run_id'])}"}])
    return TelegramReply("\n".join(lines), {"inline_keyboard": keyboard})


async def _guided_group_setup_command(database, token, message, chat, chat_id, text, now_ms):
    if not re.fullmatch(r"\s*/group_setup(?:@\w+)?\s*", text, re.IGNORECASE):
        return None
    if chat.get("type") not in {"group", "supergroup"}:
        return None
    user_id = (message.get("from") or {}).get("id")
    if type(user_id) is not int or not await is_group_admin_or_representative(database, token, chat_id, user_id):
        return "Only a registered class rep or group admin can review course setup."
    try:
        state = await group_setup_service.begin_setup_run(database, chat_id, user_id, now_ms)
    except Exception:
        logger.exception("guided_group_setup_failed chat_id=%s", chat_id)
        return "I couldn't check the group's course setup right now. Please try again shortly."
    if state.get("status") == "unlinked":
        return "This group isn't linked to a college yet. Run <code>/group_id</code>, then send <code>/group_setup CHAT_ID INVITE_CODE</code> to me privately."
    if state.get("status") == "semester_required":
        return TelegramReply(
            "The active semester isn't set. Choose the term used by this group, then I’ll review its course topics.",
            {"inline_keyboard": [[{"text": "📅 Set active semester", "callback_data": "setup:semester"}]]},
        )
    if state.get("status") != "ready" or state.get("run_id") is None:
        return "I couldn't start the setup review. Please try again shortly."
    return _setup_preview_reply(state)


async def _handle_group_setup_callback_update(database, token, update, now_ms):
    callback = update.get("callback_query")
    if not isinstance(callback, dict) or not str(callback.get("data", "")).startswith("setup:"):
        return None
    callback_id = str(callback.get("id", ""))
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    user_id = (callback.get("from") or {}).get("id")
    data = str(callback.get("data", ""))
    if not callback_id:
        return "__QUIZ_HANDLED__"
    if chat.get("type") not in {"group", "supergroup"} or type(user_id) is not int:
        await answer_telegram_callback(token, callback_id, "Use course setup in its class group.")
        return "__QUIZ_HANDLED__"
    if not await is_group_admin_or_representative(database, token, chat_id, user_id):
        await answer_telegram_callback(token, callback_id, "Only a class rep or group admin can change course mappings.")
        return "__QUIZ_HANDLED__"
    if data == "setup:semester":
        if not await is_telegram_chat_admin(token, chat_id, user_id):
            await answer_telegram_callback(token, callback_id, "Only a Telegram group admin can set the semester.")
            return "__QUIZ_HANDLED__"
        await answer_telegram_callback(token, callback_id)
        await send_telegram_message(token, {
            "chat_id": chat_id,
            "thread_id": message.get("message_thread_id"),
            "message_text": HOME_SETUP_SEMESTER_PROMPT,
            "reply_markup": {"force_reply": True, "input_field_placeholder": "e.g. Fall 2026"},
        })
        return "__QUIZ_HANDLED__"
    await answer_telegram_callback(token, callback_id)
    try:
        response = await _execute_group_setup_callback(database, chat_id, user_id, data, now_ms)
    except Exception:
        logger.exception("guided_group_setup_action_failed chat_id=%s", chat_id)
        response = "I couldn't apply that setup action. Run /group_setup to review the current mappings."
    await send_telegram_message(token, {
        "chat_id": chat_id, "thread_id": message.get("message_thread_id"), "message_text": response,
    })
    return "__QUIZ_HANDLED__"


async def _execute_group_setup_callback(database, chat_id, user_id, data, now_ms):
    apply_match = re.fullmatch(r"setup:apply:(\d{1,16})", data)
    if apply_match:
        run_id = int(apply_match.group(1))
        if not await _is_latest_setup_run(database, chat_id, run_id):
            return "This setup preview has expired. Run /group_setup to review the latest state."
        state = await group_setup_service.get_group_setup_state(database, chat_id)
        decisions = group_setup_service.classify_topic_matches(
            state.get("topics", []), state.get("offerings", [])
        )
        result = await group_setup_service.apply_exact_matches(
            database, run_id, user_id,
            [item for item in decisions if item.get("status") == "auto_apply"], now_ms,
        )
        if result.get("status") == "completed":
            return f"✅ Mapped {result.get('applied_count', 0)} clear course topic(s). Any uncertain matches were left for you to choose."
        return "The semester or topic mappings changed since this preview. Run /group_setup to review again."

    choose_match = re.fullmatch(r"setup:choose:(\d{1,16}):(\d{1,16}):(\d{1,16})", data)
    if choose_match:
        run_id, thread_id, course_id = (int(value) for value in choose_match.groups())
        if not await _is_latest_setup_run(database, chat_id, run_id):
            return "This setup preview has expired. Run /group_setup to review the latest state."
        applied = await group_setup_service.resolve_topic_match(
            database, run_id, thread_id, course_id, user_id, now_ms,
        )
        return "✅ Course topic mapped." if applied else "That match is no longer current. Run /group_setup to review again."

    undo_match = re.fullmatch(r"setup:undo:(\d{1,16})", data)
    if undo_match:
        run_id = int(undo_match.group(1))
        latest = await database.prepare(
            "SELECT id FROM group_setup_runs WHERE chat_id = ? ORDER BY id DESC LIMIT 1"
        ).bind(chat_id).first()
        if latest and int(latest.get("id", 0)) == run_id:
            result = await group_setup_service.undo_latest_setup_run(database, chat_id, user_id, now_ms)
            if result.get("status") == "undone":
                return f"↩️ Undid {result.get('undone_count', 0)} mapping change(s)."
            return "Nothing was changed; the mapping has since been edited or the setup is already undone."
    return "That setup action is no longer available. Run /group_setup to refresh it."


async def _is_latest_setup_run(database, chat_id, run_id):
    latest = await database.prepare(
        "SELECT id FROM group_setup_runs WHERE chat_id = ? ORDER BY id DESC LIMIT 1"
    ).bind(chat_id).first()
    return bool(latest and int(latest.get("id", 0)) == run_id)


async def _course_map_command(database, token: str, message: dict, chat: dict, chat_id: str, text: str, now_ms: int) -> str | None:
    if not re.match(r"^\s*/course_map(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return None
    if chat.get("type") not in {"group", "supergroup"}:
        return "Map a course from its class forum topic."
    thread_id = message.get("message_thread_id")
    course_name = parse_course_map_command(text)
    user_id = (message.get("from") or {}).get("id")
    if not thread_id or not course_name:
        return "Use <code>/course_map Course Name</code> inside the course forum topic."
    if not user_id or not await map_course_to_topic(
        database,
        token,
        chat_id,
        int(thread_id),
        course_name,
        int(user_id),
        now_ms,
    ):
        return "Only a registered group admin or representative can map a course topic."
    return f"✅ This topic is now mapped to <b>{escape(course_name)}</b>."


async def _semester_set_command(database, token: str, message: dict, chat: dict, chat_id: str, text: str, now_ms: int) -> str | None:
    if not re.match(r"^\s*/semester_set(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return None
    if chat.get("type") not in {"group", "supergroup"}:
        return "Set the semester inside your class group."
    semester = course_quiz_service.parse_semester_set_command(text)
    if not semester:
        return "Use <code>/semester_set Fall 2026</code> with a label up to 40 characters."
    user_id = (message.get("from") or {}).get("id")
    if not user_id or not await is_telegram_chat_admin(token, chat_id, int(user_id)):
        return "Only a Telegram group admin can change the current semester."
    if not await course_quiz_service.set_group_semester(database, chat_id, semester, now_ms):
        return "Set up this class group with the college before adding its semester."
    return f"✅ Current semester set to <b>{escape(semester)}</b>. Run <code>/group_setup</code> to review topic matches, or map an older topic with <code>/course_map Course Name</code>."


async def _quiz_topic_command(database, token: str, message: dict, chat: dict, chat_id: str, text: str, now_ms: int) -> str | None:
    if not re.match(r"^\s*/quiz_topic(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return None
    if chat.get("type") not in {"group", "supergroup"}:
        return "Run <code>/quiz_topic</code> inside your class group's Quiz forum topic."
    thread_id = message.get("message_thread_id")
    if not isinstance(thread_id, int) or thread_id <= 0:
        return "Run <code>/quiz_topic</code> inside the Quiz forum topic, not in General."
    user_id = (message.get("from") or {}).get("id")
    if not user_id or not await is_telegram_chat_admin(token, chat_id, int(user_id)):
        return "Only a Telegram group admin can set the quiz destination."
    group = await database.prepare(
        "SELECT college_id FROM academic_groups WHERE chat_id = ? AND active = 1"
    ).bind(chat_id).first()
    if not group or not await course_quiz_service.set_quiz_topic(
        database, chat_id, thread_id, group["college_id"], int(user_id), now_ms
    ):
        return "Set up this class group with the college before configuring its Quiz topic."
    return "✅ Automatic quizzes will be posted in this forum topic."


async def _quiz_source_command(database, token: str, message: dict, chat: dict, chat_id: str, text: str, now_ms: int) -> str | None:
    if not re.match(r"^\s*/quiz_source(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return None
    if chat.get("type") not in {"group", "supergroup"}:
        return "Mark a text source by replying to it in its mapped course topic with <code>/quiz_source</code>."
    user_id = (message.get("from") or {}).get("id")
    thread_id = message.get("message_thread_id")
    source_message = message.get("reply_to_message")
    if not user_id or not thread_id or not isinstance(source_message, dict):
        return "Reply to a text note in its course topic with <code>/quiz_source</code>."
    if not await is_group_admin_or_representative(database, token, chat_id, int(user_id)):
        return "Only a registered class rep or Telegram group admin can mark quiz notes."
    saved = await course_quiz_service.save_quiz_text_source(
        database, chat_id, int(thread_id), source_message, int(user_id), now_ms
    )
    if not saved:
        return "I couldn't save that note. Check that it is text in a mapped course topic for the current semester."
    return "✅ Note saved as material for this course's automatic quizzes."


async def _quiz_status_command(database, token: str, message: dict, chat: dict, chat_id: str, text: str, _now_ms: int) -> str | None:
    if not re.fullmatch(r"\s*/quiz_status(?:@\w+)?\s*", text, re.IGNORECASE):
        return None
    user_id = (message.get("from") or {}).get("id")
    if chat.get("type") not in {"group", "supergroup"} or not user_id or not await is_telegram_chat_admin(token, chat_id, int(user_id)):
        return "Only a Telegram group admin can view quiz pipeline status."
    group = await database.prepare(
        "SELECT current_semester FROM academic_groups WHERE chat_id = ? AND active = 1"
    ).bind(chat_id).first()
    if not group:
        return "This group is not linked to the college pilot."
    topic = await database.prepare(
        "SELECT thread_id FROM quiz_topic_settings WHERE chat_id = ?"
    ).bind(chat_id).first()
    counts = await database.prepare(
        "SELECT extraction_state, COUNT(*) AS count FROM course_quiz_sources WHERE chat_id = ? AND semester_label = ? GROUP BY extraction_state"
    ).bind(chat_id, group.get("current_semester") or "").all()
    states = {row["extraction_state"]: int(row["count"]) for row in counts["results"] or []}
    latest = await database.prepare(
        "SELECT status, last_error FROM quiz_generation_runs WHERE chat_id = ? ORDER BY created_at_ms DESC LIMIT 1"
    ).bind(chat_id).first()
    usage = await course_quiz_service.daily_ai_usage(database, int(datetime.now(timezone.utc).timestamp() * 1000))
    semester = escape(group.get("current_semester") or "not set")
    destination = "configured" if topic else "not configured"
    run_text = f"\nLatest run: {escape(latest['status'])}" if latest else "\nLatest run: none yet"
    error_text = f" ({escape(latest['last_error'])})" if latest and latest.get("last_error") else ""
    return (
        f"<b>Automatic quiz status</b>\nSemester: {semester}\nQuiz topic: {destination}\n"
        f"Sources: {states.get('pending', 0)} pending, {states.get('ready', 0)} ready, "
        f"{states.get('ignored', 0)} skipped, {states.get('failed', 0)} failed\n"
        f"AI use today (UTC): {usage['extraction_calls']}/{course_quiz_service.DAILY_EXTRACTION_CALL_LIMIT} extractions, "
        f"{usage['generation_calls']}/{course_quiz_service.DAILY_GENERATION_CALL_LIMIT} generations{run_text}{error_text}"
    )


async def _representative_command(database, token: str, message: dict, chat: dict, chat_id: str, text: str, now_ms: int) -> str | None:
    match = re.fullmatch(r"\s*/rep_(add|remove)(?:@\w+)?\s*", text, re.IGNORECASE)
    if not match:
        return None
    if chat.get("type") not in {"group", "supergroup"}:
        return "Manage class representatives from the linked class group."
    admin_id = (message.get("from") or {}).get("id")
    target_id = ((message.get("reply_to_message") or {}).get("from") or {}).get("id")
    if not admin_id or not await is_telegram_chat_admin(token, chat_id, int(admin_id)):
        return "Only a Telegram group admin can manage class representatives."
    if not target_id:
        return "Reply to the student's message with <code>/rep_add</code> or <code>/rep_remove</code>."
    if match.group(1).lower() == "add":
        changed = await add_group_representative(database, chat_id, int(target_id), int(admin_id), now_ms)
        return "✅ Class representative added." if changed else "Link this group to a college before assigning representatives."
    changed = await remove_group_representative(database, chat_id, int(target_id))
    return "Class representative removed." if changed else "That user is not an active representative here."


async def _digest_subscription_command(database, message: dict, chat: dict, chat_id: str, text: str, now_ms: int) -> str | None:
    digest_command = re.fullmatch(r"\s*/digest_(on|off)(?:@\w+)?\s*", text, re.IGNORECASE)
    if digest_command:
        if chat.get("type") not in {"group", "supergroup"}:
            return "Use <code>/digest_on</code> or <code>/digest_off</code> in your class group."
        user_id = (message.get("from") or {}).get("id")
        if not user_id:
            return "I couldn't identify your Telegram account."
        active = digest_command.group(1).lower() == "on"
        configured = await set_digest_subscription(
            database,
            int(user_id),
            chat_id,
            message.get("message_thread_id"),
            active,
            now_ms,
        )
        if not configured:
            return "Start the bot privately first and use digests in a college-linked class group."
        return "✅ Weekly digest enabled for this group/topic." if active else "Weekly digest disabled for this group/topic."
    return None


async def _resource_search_command(database, message: dict, chat: dict, chat_id: str, text: str) -> str | None:
    search_command = re.fullmatch(r"\s*/resources(?:@\w+)?(?:\s+(.+?))?\s*", text, re.IGNORECASE)
    if search_command:
        if chat.get("type") not in {"group", "supergroup"}:
            return "Search course materials in your class group."
        query = (search_command.group(1) or "").strip()
        if not query:
            return "Use <code>/resources topic or keyword</code> to search this group."
        return await search_group_resources(
            database,
            chat_id,
            query,
            message.get("message_thread_id"),
        )
    return None


def _format_personal_time(timestamp_ms: int, timezone_name: str) -> str:
    try:
        zone = personal_task_service.get_timezone(timezone_name)
    except ValueError:
        zone = personal_task_service.get_timezone(personal_task_service.DEFAULT_TIMEZONE)
    local_time = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc).astimezone(zone)
    return escape(local_time.strftime("%a, %b %d at %I:%M %p"))


def _personal_task_list_reply(tasks: list[dict], timezone_name: str) -> PersonalTaskReply:
    rows = []
    for task in tasks:
        line = f"• <b>{escape(str(task.get('title') or 'Untitled task'))}</b>"
        if task.get("due_at_ms") is not None:
            line += f"\n  Due { _format_personal_time(task['due_at_ms'], timezone_name) }"
        rows.append(line)
    text = "<b>Your tasks</b>\n\n" + "\n\n".join(rows) if rows else "<b>Your tasks</b>\n\nNo open tasks yet. Add one whenever you need it."
    keyboard = []
    for task in tasks:
        task_id = int(task["id"])
        keyboard.append([
            {"text": f"✅ {str(task.get('title') or 'Task')[:24]}", "callback_data": f"pt:c:{task_id}"},
            {"text": "🕒 Reschedule", "callback_data": f"pt:r:{task_id}"},
        ])
        keyboard.append([
            {"text": "✏️ Edit title", "callback_data": f"pt:e:{task_id}"},
            {"text": "⏸ Cancel", "callback_data": f"pt:x:{task_id}"},
        ])
        keyboard.append([
            {"text": "🗑 Delete", "callback_data": f"pt:d:{task_id}"},
        ])
    keyboard.append([
        {"text": "⏰ Add reminder", "callback_data": "home:add_reminder"},
        {"text": "🌐 Timezone", "callback_data": "home:timezone"},
    ])
    return PersonalTaskReply(text, {"inline_keyboard": keyboard})


async def _personal_task_command(database, message: dict, chat: dict, text: str, now_ms: int):
    match = re.fullmatch(r"\s*/(tasks|timezone|forget_tasks)(?:@\w+)?(?:\s+(.+?))?\s*", text, re.IGNORECASE)
    if not match:
        return None
    if chat.get("type") != "private":
        return "For privacy, manage personal tasks and reminders in a private chat with me."
    user_id = (message.get("from") or {}).get("id")
    if type(user_id) is not int or user_id <= 0:
        return "I couldn't identify your Telegram account."
    command, argument = match.group(1).lower(), (match.group(2) or "").strip()
    if command == "tasks":
        tasks = await personal_task_service.list_tasks(database, user_id, "open", 10)
        timezone_name = await personal_task_service.get_user_timezone(database, user_id)
        return _personal_task_list_reply(tasks, timezone_name)
    if command == "timezone":
        if argument:
            if not await personal_task_service.set_user_timezone(database, user_id, argument, now_ms):
                return "That timezone isn't a valid IANA name. Try <code>/timezone Africa/Addis_Ababa</code>."
        timezone_name = await personal_task_service.get_user_timezone(database, user_id)
        return f"Your reminder timezone is <code>{escape(timezone_name)}</code>. Use <code>/timezone Area/City</code> to change it."
    if argument:
        return "Use <code>/forget_tasks</code> without text, then confirm with the button."
    return PersonalTaskReply(
        "Delete all of your personal tasks and reminders? This cannot be undone.",
        {"inline_keyboard": [[
            {"text": "Delete all tasks", "callback_data": "pt:forget:yes"},
            {"text": "Keep them", "callback_data": "pt:forget:no"},
        ]]},
    )


async def _handle_private_task_message(database, update_id: int, message: dict, chat: dict, text: str, now_ms: int, ai_binding):
    if chat.get("type") != "private":
        return None
    user_id = (message.get("from") or {}).get("id")
    if type(user_id) is not int or user_id <= 0:
        return None
    reply_text = ((message.get("reply_to_message") or {}).get("text") or "").strip()
    if HOME_TIMEZONE_PROMPT in reply_text:
        changed = await personal_task_service.set_user_timezone(database, user_id, text, now_ms)
        if not changed:
            return "That timezone isn't valid. Use an IANA name such as <code>Africa/Addis_Ababa</code> or <code>Europe/London</code>."
        return f"✅ Reminder timezone set to <code>{escape(text.strip())}</code>."

    pending = None
    pending_candidate = None
    if reply_text:
        pending_candidate = await personal_task_service.peek_pending_action(database, user_id, now_ms)
    pending_prompt_matches = bool(
        pending_candidate
        and reply_text in {
            str(pending_candidate.get("clarification") or ""),
            HOME_RESCHEDULE_PROMPT if pending_candidate.get("action") == "reschedule_task" else "",
            HOME_RENAME_PROMPT if pending_candidate.get("action") == "rename_task" else "",
        }
    )
    if reply_text == HOME_TASK_PROMPT or pending_prompt_matches:
        pending = await personal_task_service.consume_pending_action(database, user_id, now_ms)
    if not pending and not personal_task_service.looks_like_task_request(text) and reply_text != HOME_TASK_PROMPT:
        return None

    timezone_name = await personal_task_service.get_user_timezone(database, user_id)
    parse_text = text
    pending_action = str((pending or {}).get("action") or "")
    if pending_action == "clarification":
        original = str((pending or {}).get("original_text") or "").strip()
        parse_text = f"Original request: {original}\nClarification answer: {text}" if original else text
    elif pending_action == "reschedule_task":
        parse_text = f"Remind me to complete this task {text}"
    elif pending_action == "rename_task":
        task_id = int((pending or {}).get("task_id") or 0)
        renamed = await personal_task_service.rename_task(database, user_id, task_id, text, now_ms)
        return "✅ Task title updated." if renamed else "I couldn't update that task. Open <b>My tasks</b> and try again."

    intent = await personal_task_service.parse_task_intent(ai_binding, parse_text, timezone_name, now_ms)
    if pending_action == "reschedule_task":
        if intent.action != "create_task" or intent.reminder_at_ms is None:
            return PersonalTaskReply(
                "I couldn't identify the new reminder time. Please reply with a clear date and time.",
                {"force_reply": True, "input_field_placeholder": "Tomorrow at 5 PM"},
            )
        task_id = int((pending or {}).get("task_id") or 0)
        updated = await personal_task_service.reschedule_task(
            database, user_id, task_id, intent.reminder_at_ms, now_ms
        )
        return "✅ Reminder rescheduled." if updated else "I couldn't find that task in your account."
    if intent.action == "not_task_request":
        return None
    if intent.action == "clarification":
        await personal_task_service.save_pending_action(
            database, user_id, update_id, intent, now_ms, original_text=parse_text
        )
        return PersonalTaskReply(
            escape(intent.clarification or "What date and time should I use?"),
            {"force_reply": True, "input_field_placeholder": "Type your answer…"},
        )
    if intent.action == "create_task":
        task = await personal_task_service.create_task(
            database, user_id, update_id, intent, timezone_name, now_ms
        )
        if not task:
            return "I couldn't save that task right now. Please try again."
        if intent.reminder_at_ms is not None:
            when = _format_personal_time(intent.reminder_at_ms, timezone_name)
            return f"✅ <b>Reminder set</b>\n{escape(task['title'])}\n📅 {when} ({escape(timezone_name)})"
        when = _format_personal_time(intent.due_at_ms, timezone_name) if intent.due_at_ms else None
        return f"✅ <b>Task saved</b>\n{escape(task['title'])}" + (f"\n📅 Due {when}" if when else "")
    if intent.action == "list_tasks":
        tasks = await personal_task_service.list_tasks(database, user_id, "open", 10)
        return _personal_task_list_reply(tasks, timezone_name)
    if intent.action == "delete_all_tasks":
        return await _personal_task_command(database, message, chat, "/forget_tasks", now_ms)
    if intent.action in {"complete_task", "cancel_task", "delete_task", "reschedule_task", "rename_task"}:
        return "Open <b>My tasks</b> and choose the task button so I update the right one."
    return None


async def _basic_command(database, message: dict, chat: dict, chat_id: str, text: str, now_ms: int) -> str | None:
    if re.fullmatch(r"\s*/group_id(?:@\w+)?\s*", text, re.IGNORECASE):
        if chat.get("type") not in {"group", "supergroup"}:
            return "Use <code>/group_id</code> inside your class group."
        return f"Group ID: <code>{escape(chat_id)}</code>. Send it privately with your college invite code to link this group."
    digest_reply = await _digest_subscription_command(database, message, chat, chat_id, text, now_ms)
    if digest_reply is not None:
        return digest_reply
    resource_reply = await _resource_search_command(database, message, chat, chat_id, text)
    if resource_reply is not None:
        return resource_reply
    if re.match(r"^/start(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return _welcome_message(chat.get("type", "private"))
    if re.match(r"^/help(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return _help_message(chat.get("type", "private"))
    if re.match(r"^/upcoming(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return await _upcoming_message(database, chat_id, now_ms)
    return None


async def _record_private_start(database, message: dict, chat: dict, text: str, now_ms: int) -> None:
    if chat.get("type") != "private" or not re.match(r"^/start(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return
    user_id = (message.get("from") or {}).get("id")
    if user_id:
        await register_private_bot_user(database, int(user_id), now_ms)


async def _save_resource_update(database, chat_id: str, message: dict, text_value, now_ms: int) -> tuple[bool, str | None]:
    resource = extract_telegram_resource(message)
    if not resource:
        return False, None
    saved = await save_course_resource(database, chat_id, resource, now_ms)
    quiz_source = await course_quiz_service.save_quiz_file_source(database, chat_id, resource, now_ms)
    if text_value is not None:
        return saved or quiz_source, None
    if not saved and not quiz_source:
        return False, "This class group is not connected to a college yet, so I couldn't save this material."
    return True, "📚 Course material saved."


async def _handle_quiz_text_command(
    database, token: str, message: dict, chat: dict, chat_id: str, text: str, now_ms: int,
    bot_username: str,
) -> str | None:
    command = re.match(r"\s*/(quiz_create|quiz_question|quiz_publish|quiz_results)(?:@\w+)?(?:\s|$)", text, re.IGNORECASE)
    if not command:
        return None
    if chat.get("type") not in {"group", "supergroup"}:
        return "Create and manage quizzes in the mapped course topic of your class group."
    if command.group(1).lower() == "quiz_create":
        return "Manual private quiz creation is retired. Set up the semester and Quiz topic to use automatic course polls."
    user_id = (message.get("from") or {}).get("id")
    if not user_id:
        return "I couldn't identify your Telegram account."
    if not await is_group_admin_or_representative(database, token, chat_id, int(user_id)):
        return "Only registered class reps and group admins can manage quizzes."
    command_name = command.group(1).lower()
    if command_name == "quiz_create":
        return await _create_quiz_command(database, message, chat_id, text, int(user_id), now_ms)
    if command_name == "quiz_question":
        return await _add_quiz_question_command(database, chat_id, text, now_ms)
    quiz_id = quiz_service.parse_quiz_id_command(text, command_name)
    if not quiz_id:
        return f"Use <code>/{command_name} QUIZ_ID</code>."
    if command_name == "quiz_publish":
        return await _publish_quiz_command(database, chat_id, quiz_id, now_ms, bot_username)
    return await _quiz_results_command(database, chat_id, quiz_id)


async def _handle_class_poll_command(database, token, update_id, message, chat, chat_id, text, now_ms):
    if not re.match(r"\s*/class_poll(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
        return None
    if chat.get("type") not in {"group", "supergroup"}:
        return "Post class polls from a registered class group."
    user_id = (message.get("from") or {}).get("id")
    if not user_id or not await is_group_admin_or_representative(
        database, token, chat_id, int(user_id)
    ):
        return "Only registered class reps and group admins can post class polls."
    poll = poll_service.parse_class_poll_command(text)
    if not poll:
        return "Use <code>/class_poll Question | Option A | Option B [| more options]</code> (2–10 distinct options)."
    poll["chat_id"] = chat_id
    poll["thread_id"] = message.get("message_thread_id")
    posted = await send_class_poll_once(database, token, update_id, poll, now_ms)
    if not posted:
        return "This poll command was already handled. Check the topic before trying again."
    return "📊 Anonymous poll posted. Telegram will collect the votes."


async def _create_quiz_command(database, message, chat_id, text, user_id, now_ms):
    title = quiz_service.parse_quiz_create_command(text)
    if not title:
        return "Use <code>/quiz_create Midterm Practice</code> inside a mapped course topic."
    thread_id = message.get("message_thread_id")
    if not thread_id:
        return "Create a quiz inside the mapped course topic."
    quiz_id = await quiz_service.create_quiz(database, chat_id, int(thread_id), user_id, title, now_ms)
    if not quiz_id:
        return "This topic is not mapped to an active course. Use <code>/course_map Course Name</code> first."
    return f"Draft quiz <b>#{quiz_id}</b> created. Add questions with <code>/quiz_question {quiz_id} | Question | Option A | Option B | Option C | Option D | 1-4 | explanation</code>."


async def _add_quiz_question_command(database, chat_id, text, now_ms):
    parsed = quiz_service.parse_quiz_question_command(text)
    if not parsed:
        return "Use <code>/quiz_question ID | Question | A | B | C | D | correct option 1-4 | optional explanation</code>."
    question_id = await quiz_service.add_quiz_question(database, chat_id, parsed["quiz_id"], parsed, now_ms)
    return f"✅ Question {question_id} added." if question_id else "That quiz is missing, published, or already has 20 questions."


async def _publish_quiz_command(database, chat_id, quiz_id, now_ms, bot_username):
    published = await quiz_service.publish_quiz(database, chat_id, quiz_id, now_ms)
    if not published:
        return "Quiz not found, already published, or has no questions."
    link = f"https://t.me/{bot_username}?start=quiz_{quiz_id}" if bot_username else f"Open the bot privately and send /start quiz_{quiz_id}."
    return f"✅ <b>{escape(published['title'])}</b> is live. Students can open it privately: {escape(link)}"


async def _quiz_results_command(database, chat_id, quiz_id):
    summary = await quiz_service.quiz_results_summary(database, chat_id, quiz_id)
    if not summary:
        return "Published quiz not found in this group."
    return (
        f"<b>{escape(summary['title'])} — aggregate results</b>\n"
        f"Completed attempts: {summary['attempt_count']}\n"
        f"Average score: {float(summary['average_percent']):.1f}%"
    )


async def _start_private_quiz(database, token: str, message: dict, payload: str, now_ms: int) -> str:
    quiz_id = quiz_service.parse_quiz_start_payload(payload)
    user_id = (message.get("from") or {}).get("id")
    if not quiz_id or not user_id:
        return "That quiz link is invalid. Ask your class rep for a fresh link."
    quiz = await quiz_service.get_published_quiz(database, quiz_id)
    if not quiz:
        return "That quiz is no longer available."
    if not await is_telegram_chat_member(token, quiz["chat_id"], int(user_id)):
        return "Join the class group first, then reopen this quiz link."
    attempt_id = await quiz_service.start_quiz_attempt(database, quiz_id, int(user_id), now_ms)
    question = await quiz_service._next_question(database, attempt_id) if attempt_id else None
    if not question:
        result = await quiz_service._complete_attempt(database, attempt_id, now_ms) if attempt_id else None
        if result:
            return f"You scored <b>{result['score']}/{result['total_questions']}</b>. Your result is private."
        return "I couldn't start this quiz. Please try again."
    prompt = _quiz_question_message(quiz, question, attempt_id)
    await send_telegram_message(token, {**prompt, "chat_id": str(message["chat"]["id"])})
    return ""


async def _process_quiz_callback(database, token: str, callback: dict, now_ms: int) -> None:
    callback_id = str(callback.get("id", ""))
    user = callback.get("from") or {}
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    match = re.fullmatch(r"quiz:(\d+):(\d+):([0-3])", str(callback.get("data", "")))
    if not callback_id:
        return
    if not match or chat.get("type") != "private" or not user.get("id"):
        await answer_telegram_callback(token, callback_id, "This quiz action is invalid.")
        return
    attempt_id, question_id, selected = (int(value) for value in match.groups())
    result = await quiz_service.submit_quiz_answer(
        database, attempt_id, question_id, selected, int(user["id"]), now_ms
    )
    if not result:
        await answer_telegram_callback(token, callback_id, "This quiz is no longer available.")
        return
    await answer_telegram_callback(token, callback_id)
    if result["status"] == "completed":
        await send_telegram_message(token, {
            "chat_id": str(chat["id"]),
            "message_text": f"Quiz complete. You scored <b>{result['score']}/{result['total_questions']}</b>. Your result is private.",
        })
        return
    attempt = await database.prepare(
        "SELECT q.id, q.title, q.college_id, q.chat_id, q.thread_id, q.course_id, c.course_name FROM quiz_attempts a JOIN quiz_sets q ON q.id = a.quiz_id JOIN courses c ON c.id = q.course_id WHERE a.id = ?"
    ).bind(attempt_id).first()
    prompt = _quiz_question_message(attempt, result["question"], attempt_id)
    await send_telegram_message(token, {**prompt, "chat_id": str(chat["id"])})


async def _handle_text_command(
    database,
    token: str,
    update_id: int,
    message: dict,
    chat: dict,
    chat_id: str,
    text: str,
    now_ms: int,
    ai_binding=None,
    search_api_key: str = "",
    lesson_base_url: str = "",
) -> str | None:
    tutor_reply = await _course_tutor_command(
        database, message, chat, chat_id, text, now_ms, ai_binding, search_api_key,
        lesson_base_url,
    )
    if tutor_reply is not None:
        return tutor_reply
    course_quiz_reply = await _course_quiz_command(database, token, message, chat, chat_id, text, now_ms)
    if course_quiz_reply is not None:
        return course_quiz_reply
    setup_reply = await _group_setup_command(database, token, message, chat, text, now_ms)
    if setup_reply is not None:
        return setup_reply
    course_reply = await _course_map_command(database, token, message, chat, chat_id, text, now_ms)
    if course_reply is not None:
        return course_reply
    representative_reply = await _representative_command(
        database, token, message, chat, chat_id, text, now_ms
    )
    if representative_reply is not None:
        return representative_reply
    basic_reply = await _basic_command(database, message, chat, chat_id, text, now_ms)
    if basic_reply is not None:
        return basic_reply
    approved = await _approve_command(database, token, message, chat, chat_id, text, now_ms)
    if approved is not None:
        return approved
    event = parse_academic_command(text)
    if event:
        return await _explicit_event_command(
            database, token, message, chat, chat_id, event, update_id, now_ms
        )
    return None


async def _course_quiz_command(database, token, message, chat, chat_id, text, now_ms):
    for command in (
        _semester_set_command,
        _quiz_topic_command,
        _quiz_source_command,
        _quiz_status_command,
    ):
        reply = await command(database, token, message, chat, chat_id, text, now_ms)
        if reply is not None:
            return reply
    return None


def _instant_view_url(lesson_url: str, iv_rhash: str = "") -> str:
    if iv_rhash and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", iv_rhash):
        return "https://t.me/iv?url=" + quote(lesson_url, safe="") + "&rhash=" + iv_rhash
    return lesson_url


def _batch_sender(token: str, message: dict):
    """Send each worksheet answer as a new Telegram message."""
    chat_id = str((message.get("chat") or {}).get("id", ""))
    thread_id = message.get("message_thread_id")

    async def send(text: str, reply_markup):
        return await send_telegram_message(token, {
            "chat_id": chat_id, "thread_id": thread_id, "message_text": text,
            "reply_markup": reply_markup, "disable_link_preview": True,
        })

    async def clear(message_id) -> None:
        await clear_telegram_buttons(token, chat_id, message_id)
    send.clear = clear
    return send


def _batch_lesson_maker(database, now_ms: int, lesson_base_url: str, iv_rhash: str = ""):
    if not lesson_base_url:
        return None

    async def make(title: str, text: str):
        url = await lesson_page_service.create_visual_lesson(
            database, f"Solutions: {title}", text, [], now_ms, lesson_base_url
        )
        return _instant_view_url(url, iv_rhash) if url else None
    return make


async def _run_exercise_batch(
    database, token, message, ai_binding, user_id, chat_id, document, numbers,
    now_ms, lesson_base_url, iv_rhash="",
):
    try:
        preferences = await course_tutor_service.get_tutor_preferences(database, user_id)
    except Exception:
        preferences = ""
    return await batch_exercises.run_batch(
        database=database, ai_binding=ai_binding, send=_batch_sender(token, message),
        split=split_telegram_html, user_id=user_id, chat_id=chat_id, document=document, numbers=numbers,
        make_lesson=_batch_lesson_maker(database, now_ms, lesson_base_url, iv_rhash),
        study_preferences=preferences,
    )


async def _handle_exercise_callback_update(
    database, token, update, now_ms, ai_binding, lesson_base_url="", iv_rhash="",
):
    callback = update.get("callback_query")
    if not isinstance(callback, dict) or not str(callback.get("data", "")).startswith("ex:"):
        return None
    callback_id = str(callback.get("id", ""))
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    user_id = (callback.get("from") or {}).get("id")
    if callback_id:
        await answer_telegram_callback(token, callback_id)
    parts = str(callback.get("data", "")).split(":")
    if chat.get("type") != "private" or not chat_id or type(user_id) is not int or str(user_id) != chat_id or len(parts) != 3 or ai_binding is None:
        return "__QUIZ_HANDLED__"
    send = _batch_sender(token, message)
    document = await batch_exercises.load_document(database, user_id, chat_id, now_ms)
    if not document:
        await send("That exercise sheet has expired. Please send it again.", None)
        return "__QUIZ_HANDLED__"
    action = parts[1]
    number = int(parts[2]) if parts[2].isdigit() else 0
    if action in {"r", "c"}:
        todo = [n for n, _ in document["questions"] if n not in document["done"]]
        if not todo:
            await send("Nothing left to retry.", None)
            return "__QUIZ_HANDLED__"
        return await _run_exercise_batch(
            database, token, message, ai_binding, user_id, chat_id, document, todo,
            now_ms, lesson_base_url, iv_rhash,
        )
    try:
        preferences = await course_tutor_service.get_tutor_preferences(database, user_id)
    except Exception:
        preferences = ""
    return await batch_exercises.run_followup(
        database=database, ai_binding=ai_binding, send=send, split=split_telegram_html,
        user_id=user_id, chat_id=chat_id, document=document, action=action, number=number,
        study_preferences=preferences,
    )


async def _tutor_buttons(database, ai_binding, user_id, chat_id, question, reply, now_ms, iv_rhash=""):
    truncated = bool(getattr(reply, "truncated", False))
    lesson_url = None
    text = reply
    if isinstance(reply, course_tutor_service.TutorReply):
        lesson_url = _instant_view_url(reply.lesson_url, iv_rhash)
        text = reply.text
    items = []
    key = followups.new_key()
    if isinstance(text, str) and len(text) > 200 and type(user_id) is int:
        items = await followups.suggest(ai_binding, question, text)
        if items:
            try:
                await followups.save(database, user_id, chat_id, items, now_ms, key)
            except Exception:
                items = []
    return text, followups.keyboard(items, lesson_url, truncated, key)


async def _handle_followup_callback_update(
    database, token, update, now_ms, ai_binding, search_api_key="", lesson_base_url="", iv_rhash="",
):
    callback = update.get("callback_query")
    if not isinstance(callback, dict) or not str(callback.get("data", "")).startswith("fu:"):
        return None
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    user_id = (callback.get("from") or {}).get("id")
    if callback.get("id"):
        await answer_telegram_callback(token, str(callback["id"]))
    parts = str(callback.get("data")).split(":")
    if chat.get("type") != "private" or type(user_id) is not int or str(user_id) != chat_id or ai_binding is None:
        return "__QUIZ_HANDLED__"
    send = _batch_sender(token, message)
    if parts[1] == "c":
        question = followups.CONTINUE_PROMPT
    else:
        # New keyboards carry their immutable answer-set key; the old three-part format is
        # accepted long enough to give existing messages a clear expiry response.
        if len(parts) == 4:
            set_key, raw_index = parts[2], parts[3]
            items = await followups.load(database, user_id, chat_id, now_ms, set_key)
        elif len(parts) == 3:
            raw_index, items = parts[2], []
        else:
            raw_index, items = "", []
        index = int(raw_index) if raw_index.isdigit() else -1
        if not 0 <= index < len(items):
            await send("Those suggestions expired. Just type your question.", None)
            return "__QUIZ_HANDLED__"
        question = items[index]["question"]
        await send(f"💬 <i>{escape(question)}</i>", None)
    answer = await course_tutor_service.answer_general_question(
        database, ai_binding, search_api_key, chat_id, user_id, question, now_ms,
        lesson_base_url=lesson_base_url,
    )
    text, markup = await _tutor_buttons(
        database, ai_binding, user_id, chat_id, question, answer, now_ms, iv_rhash
    )
    await send(text, markup)
    return "__QUIZ_HANDLED__"


async def _exercise_text_command(
    database, token, message, chat, chat_id, text, now_ms, ai_binding, lesson_base_url, iv_rhash="",
):
    if chat.get("type") != "private" or ai_binding is None:
        return None
    if not (
        batch_exercises.wants_solve_all(text) or batch_exercises.wants_continue(text)
        or batch_exercises.question_selection(text, 30)
    ):
        return None
    user_id = (message.get("from") or {}).get("id")
    if type(user_id) is not int or str(user_id) != str(chat_id):
        return None
    document = await batch_exercises.load_document(database, user_id, chat_id, now_ms)
    if not document:
        return None
    all_numbers = [n for n, _ in document["questions"]]
    selection = batch_exercises.question_selection(text, len(all_numbers))
    if selection:
        numbers = selection
    elif batch_exercises.wants_continue(text):
        numbers = [n for n in all_numbers if n not in document["done"]]
        if not numbers:
            return None
    else:
        numbers = all_numbers
    return await _run_exercise_batch(
        database, token, message, ai_binding, user_id, chat_id, document, numbers,
        now_ms, lesson_base_url, iv_rhash,
    )


async def _process_telegram_update(
    database,
    token: str,
    update_id: int,
    update: dict,
    now_ms: int,
    ai_api_key: str = "",
    ai_model: str = "llama-3.1-8b-instant",
    bot_username: str = "",
    ai_binding=None,
    search_api_key: str = "",
    lesson_base_url: str = "",
    iv_rhash: str = "",
) -> str | None:
    """Handle supported bot commands and academic event detection."""
    home_callback_reply = await _handle_home_callback_update(database, token, update, now_ms)
    if home_callback_reply is not None:
        return home_callback_reply
    task_callback_reply = await _handle_personal_task_callback_update(database, token, update, now_ms)
    if task_callback_reply is not None:
        return task_callback_reply
    setup_callback_reply = await _handle_group_setup_callback_update(database, token, update, now_ms)
    if setup_callback_reply is not None:
        return setup_callback_reply
    callback_reply = await _handle_quiz_callback_update(database, token, update, now_ms)
    if callback_reply is not None:
        return callback_reply
    exercise_callback_reply = await _handle_exercise_callback_update(
        database, token, update, now_ms, ai_binding, lesson_base_url, iv_rhash
    )
    if exercise_callback_reply is not None:
        return exercise_callback_reply
    followup_reply = await _handle_followup_callback_update(
        database, token, update, now_ms, ai_binding, search_api_key, lesson_base_url, iv_rhash
    )
    if followup_reply is not None:
        return followup_reply
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    return await _process_message_update(
        database, token, update_id, message, now_ms, ai_api_key, ai_model, bot_username,
        ai_binding, search_api_key, lesson_base_url, iv_rhash,
    )


async def _process_message_update(
    database, token, update_id, message, now_ms, ai_api_key, ai_model, bot_username,
    ai_binding=None, search_api_key="", lesson_base_url="", iv_rhash="",
):
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text_value = message.get("text") or message.get("caption")
    topic_event = message.get("forum_topic_created") or message.get("forum_topic_edited")
    thread_id = message.get("message_thread_id")
    if (
        chat.get("type") in {"group", "supergroup"}
        and isinstance(topic_event, dict)
        and type(thread_id) is int
    ):
        try:
            sender_id = (message.get("from") or {}).get("id")
            await group_setup_service.observe_forum_topic(
                database, chat_id, thread_id, str(topic_event.get("name") or ""),
                sender_id if type(sender_id) is int else None, now_ms,
            )
        except Exception:
            logger.exception("forum_topic_observation_failed chat_id=%s thread_id=%s", chat_id, thread_id)
    if isinstance(text_value, str):
        await _record_private_start(database, message, chat, text_value, now_ms)
        if re.match(r"^/start(?:@\w+)?(?:\s|$)", text_value, re.IGNORECASE):
            await ensure_telegram_command_menu(token)
        start_reply = await _handle_private_quiz_start(database, token, message, chat, text_value, now_ms)
        if start_reply is not None:
            return start_reply
        guided_setup_reply = await _guided_group_setup_command(
            database, token, message, chat, chat_id, text_value, now_ms
        )
        if guided_setup_reply is not None:
            return guided_setup_reply
        setup_reply = await _group_setup_command(database, token, message, chat, text_value, now_ms)
        if setup_reply is not None:
            return setup_reply
    resource_saved, resource_reply = await _save_resource_update(
        database, chat_id, message, text_value, now_ms
    )
    attachment_reply = await _private_tutor_attachment_command(
        database, token, message, chat, chat_id, text_value,
        now_ms, ai_binding, search_api_key, lesson_base_url, iv_rhash,
    )
    if attachment_reply is not None:
        return attachment_reply
    raw_text = text_value
    if not isinstance(raw_text, str):
        return resource_reply
    text = raw_text
    if chat.get("type") == "private" and await _is_group_linking_request(database, message, chat_id, text):
        return _group_linking_help()
    replied_to = message.get("reply_to_message") or {}
    if replied_to.get("text") == HOME_SETUP_SEMESTER_PROMPT and chat.get("type") in {"group", "supergroup"}:
        return await _semester_set_command(
            database, token, message, chat, chat_id, f"/semester_set {text}", now_ms
        )
    if replied_to.get("text") == HOME_ASK_PROMPT and chat.get("type") in {"group", "supergroup"}:
        return await _course_tutor_command(
            database, message, chat, chat_id, f"/ask {text}", now_ms,
            ai_binding, search_api_key, lesson_base_url,
        )
    if replied_to.get("text") == HOME_RESOURCE_PROMPT:
        return await _resource_search_command(
            database, message, chat, chat_id, f"/resources {text}"
        )
    if replied_to.get("text") == HOME_REMEMBER_PROMPT:
        return await _tutor_memory_command(
            database, message, chat, f"/remember {text}", now_ms
        )
    if chat.get("type") == "private":
        exercise_reply = await _exercise_text_command(
            database, token, message, chat, chat_id, text, now_ms, ai_binding, lesson_base_url,
            iv_rhash,
        )
        if exercise_reply is not None:
            return exercise_reply
    personal_command = await _personal_task_command(database, message, chat, text, now_ms)
    if personal_command is not None:
        return personal_command
    if chat.get("type") == "private":
        task_reply = await _handle_private_task_message(
            database, update_id, message, chat, text, now_ms, ai_binding
        )
        if task_reply is not None:
            return task_reply
    memory_reply = await _tutor_memory_command(database, message, chat, text, now_ms)
    if memory_reply is not None:
        return memory_reply
    poll_reply = await _handle_class_poll_command(
        database, token, update_id, message, chat, chat_id, text, now_ms
    )
    if poll_reply is not None:
        return poll_reply
    quiz_reply = await _handle_quiz_text_command(
        database, token, message, chat, chat_id, text, now_ms, bot_username
    )
    if quiz_reply is not None:
        return quiz_reply
    command_reply = await _handle_text_command(
        database, token, update_id, message, chat, chat_id, text, now_ms,
        ai_binding, search_api_key, lesson_base_url,
    )
    if command_reply is not None:
        return command_reply
    if (
        chat.get("type") == "private"
        and not text.lstrip().startswith("/")
        and not (message.get("from") or {}).get("is_bot")
    ):
        user_id = (message.get("from") or {}).get("id")
        if type(user_id) is int:
            return await course_tutor_service.answer_general_question(
                database, ai_binding, search_api_key, chat_id, user_id, text, now_ms,
                lesson_base_url=lesson_base_url,
            )
    if chat.get("type") not in {"group", "supergroup"}:
        return None
    event = await extract_academic_event(ai_api_key, text, now_ms, ai_model)
    if not event:
        return "📚 Course material saved." if resource_saved else None
    return await _save_and_announce_ai_event(database, message, chat_id, update_id, event, now_ms)


def _group_linking_help() -> str:
    return (
        "🔗 <b>Connect your class group to Academic Assistant</b>\n\n"
        "1. Add <a href=\"https://t.me/academicgroupmanagementbot\">@academicgroupmanagementbot</a> to your Telegram class group.\n"
        "2. In that group, send <code>/group_id</code> and copy the ID it returns.\n"
        "3. Message me privately with <code>/group_setup -100… COLLEGE_INVITE_CODE</code>. "
        "A college admin must provide the invite code.\n"
        "4. After it is linked, run <code>/group_setup</code> in the group to review semester and course-topic mappings.\n\n"
        "I can guide the mapping, but I can’t join or link a group until you add me and submit its ID with a valid college invite code."
    )


async def _is_group_linking_request(database, message: dict, chat_id: str, text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9 ]", " ", str(text or "").casefold())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    direct = bool(
        re.search(r"\b(?:setup|set up|connect|link|attach)\b", normalized)
        and re.search(r"\b(?:my |the )?group\b", normalized)
    )
    contextual = bool(re.search(r"\blink it to (?:u|you|the bot|this bot)\b", normalized))
    if not direct and not contextual:
        return False
    if direct:
        return True
    user_id = (message.get("from") or {}).get("id")
    if type(user_id) is not int or str(user_id) != chat_id:
        return False
    try:
        _preferences, history = await course_tutor_service._read_tutor_context(
            database, user_id, chat_id, course_tutor_service.PRIVATE_TUTOR_CONTEXT
        )
    except Exception:
        return False
    recent_user_text = " ".join(
        str(turn.get("content") or "") for turn in history[-6:]
        if isinstance(turn, dict) and turn.get("role") == "user"
    ).casefold()
    return bool(re.search(r"\b(?:group|telegram chat)\b", recent_user_text) and re.search(r"\b(?:setup|set up|connect|link)\b", recent_user_text))


async def _private_tutor_attachment_command(
    database, token, message, chat, chat_id, caption, now_ms, ai_binding, search_api_key,
    lesson_base_url="", iv_rhash="",
) -> str | None:
    """Read a private PDF, supported document, or image for the tutor without retaining its contents."""
    if chat.get("type") != "private":
        return None
    document = message.get("document")
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        media = photos[-1]
        source_type = "photo"
        name = "uploaded-image.jpg"
        mime_type = "image/jpeg"
    elif isinstance(document, dict):
        media = document
        source_type = "document"
        name = str(document.get("file_name") or "uploaded-file").strip()[:200]
        mime_type = str(document.get("mime_type") or "").lower()
    else:
        return None

    supported_extensions = {
        ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".svg", ".gif", ".bmp",
        ".docx", ".xlsx", ".xlsm", ".xlsb", ".xls", ".et", ".ods", ".odt",
        ".pptx", ".csv", ".numbers", ".html", ".htm", ".xml",
    }
    extension = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    supported_mime_types = {
        "application/pdf", "image/jpeg", "image/png", "image/webp", "image/svg+xml",
        "image/gif", "image/bmp", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel",
        "application/vnd.ms-excel.sheet.macroenabled.12", "text/csv", "application/xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/html", "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
    }
    if source_type == "document" and extension not in supported_extensions and mime_type not in supported_mime_types:
        return (
            "I can analyze PDFs, common images, Word and PowerPoint presentations, spreadsheets, CSV, HTML, and XML. "
            "Please send one of those formats."
        )
    if not isinstance(media, dict) or not isinstance(media.get("file_id"), str):
        return "I couldn't read this attachment. Please try sending it again."
    file_size = media.get("file_size")
    if isinstance(file_size, (int, float)) and file_size > course_quiz_service.MAX_FILE_BYTES:
        return "This file is over Telegram’s 20 MB download limit for bots. Please send a smaller file."
    user_id = (message.get("from") or {}).get("id")
    if type(user_id) is not int:
        return "I couldn't identify your Telegram account for this tutor request."
    if ai_binding is None:
        return "File analysis is temporarily unavailable. Please try again shortly."

    source = {
        "file_id": media["file_id"],
        "file_size": file_size,
        "mime_type": mime_type,
        "title": name,
    }
    try:
        conversion_binding = (
            ai_binding.binding if isinstance(ai_binding, ProgressiveTutorBinding) else ai_binding
        )
        extracted_content = await course_quiz_service.extract_telegram_file_source(
            token, conversion_binding, source
        )
    except ValueError as exc:
        if str(exc) == "file_too_large":
            return "This file is over Telegram’s 20 MB download limit for bots. Please send a smaller file."
        if extension == ".pptx" and str(exc) == "empty_extraction":
            return "I opened the PowerPoint, but it has no selectable slide text. If it contains scanned slides, export it as a PDF or send the slide images."
        return "I couldn't extract readable content from that file. Try a clearer image or a text-based PDF."
    except Exception as exc:
        logger.exception(
            "private_tutor_attachment_extraction_failed extension=%s mime_type=%s error_type=%s",
            extension or "none",
            mime_type or "unknown",
            type(exc).__name__,
        )
        return (
            "I couldn't read that attachment this time. Please try sending it again; "
            "if it still fails, send the PDF or image as a file instead of a forwarded preview."
        )
    if not isinstance(extracted_content, str) or not extracted_content.strip():
        return "I couldn't find readable text or describe the image. Try a clearer image or document."

    exercises = batch_exercises.parse_exercises(extracted_content)
    caption_text = str(caption or "").strip()
    selected_questions = batch_exercises.question_selection(caption_text, len(exercises)) if exercises else []
    explicit_solve = batch_exercises.wants_solve_all(caption_text) or bool(selected_questions)
    if exercises and (batch_exercises.looks_like_exercise_sheet(extracted_content) or explicit_solve):
        await batch_exercises.save_document(
            database, user_id, chat_id, name, extracted_content, exercises,
            batch_exercises.detect_language(extracted_content), now_ms,
        )
        should_solve = (
            not batch_exercises.wants_summary(caption_text)
            and (explicit_solve or (not caption_text and batch_exercises.looks_like_exercise_sheet(extracted_content)))
        )
        if should_solve:
            document_state = await batch_exercises.load_document(database, user_id, chat_id, now_ms)
            if document_state:
                return await _run_exercise_batch(
                    database, token, message, ai_binding, user_id, chat_id, document_state,
                    selected_questions or [n for n, _ in document_state["questions"]],
                    now_ms, lesson_base_url, iv_rhash,
                )

    question = caption.strip() if isinstance(caption, str) else ""
    if len(question) > course_tutor_ai.MAX_QUERY_CHARS:
        return f"Please shorten the caption to {course_tutor_ai.MAX_QUERY_CHARS} characters or fewer and resend the attachment."
    if not question:
        question = (
            "Describe this image, transcribe any visible text, and explain the important details."
            if source_type == "photo" or mime_type.startswith("image/")
            else "Summarize this document's key ideas and explain them clearly."
        )
    attachment_context = {"name": name, "content": extracted_content}
    answer = await course_tutor_service.answer_general_question(
        database, ai_binding, search_api_key, chat_id, user_id,
        question, now_ms, attachment_context=attachment_context,
        allow_visual_lesson=True, lesson_base_url=lesson_base_url,
    )
    prefix = f"📎 <b>Analyzed: {escape(name)}</b>\n\n"
    if isinstance(answer, course_tutor_service.TutorReply):
        return course_tutor_service.TutorReply(prefix + answer.text, answer.lesson_url, answer.truncated)
    return course_tutor_ai.tag_text(prefix + answer, getattr(answer, "truncated", False))


async def _tutor_memory_command(database, message, chat, text, now_ms) -> str | None:
    """Handle user-controlled tutor preferences and personal data deletion."""
    match = re.fullmatch(r"\s*/(remember|memory|forget_me)(?:@\w+)?(?:\s+(.*?))?\s*", text, re.IGNORECASE)
    if not match:
        return None
    command = match.group(1).casefold()
    argument = (match.group(2) or "").strip()
    sender = message.get("from") or {}
    user_id = sender.get("id")
    if type(user_id) is not int:
        return "I couldn't identify your Telegram account for this memory command."
    if command == "remember":
        if not argument:
            return "Use <code>/remember preference</code>, for example <code>/remember use concise explanations</code>."
        if len(argument) > course_tutor_service.MAX_TUTOR_PREFERENCES_CHARS:
            return f"Keep a study preference under {course_tutor_service.MAX_TUTOR_PREFERENCES_CHARS} characters."
        try:
            await course_tutor_service.set_tutor_preferences(database, user_id, argument, now_ms)
        except Exception:
            return "I couldn't save that preference right now. Please try again shortly."
        return "Tutor preference saved for your account."
    if command == "memory":
        if chat.get("type") != "private":
            return "Use <code>/memory</code> privately with the bot so your preference stays private."
        try:
            preferences = await course_tutor_service.get_tutor_preferences(database, user_id)
        except Exception:
            return "I couldn't read your tutor preference right now. Please try again shortly."
        if not preferences:
            return "You haven't saved a tutor preference. Use <code>/remember preference</code> to add one."
        return "<b>Your tutor preference</b>\n" + escape(preferences)
    try:
        await course_tutor_service.forget_tutor_user(database, user_id)
    except Exception:
        return "I couldn't erase your tutor data right now. Please try again shortly."
    return "Your saved tutor conversations and preferences have been erased."


async def _course_tutor_command(
    database,
    message: dict,
    chat: dict,
    chat_id: str,
    text: str,
    now_ms: int,
    ai_binding=None,
    search_api_key: str = "",
    lesson_base_url: str = "",
) -> str | course_tutor_service.TutorReply | None:
    match = re.fullmatch(r"\s*/ask(?:@\w+)?(?:\s+(.*?))?\s*", text, re.IGNORECASE)
    if not match:
        return None
    question = (match.group(1) or "").strip()
    if not question:
        if chat.get("type") == "private":
            return "Send a question after <code>/ask</code>, or just message me privately."
        return "Use <code>/ask your question</code> in a mapped course topic."
    if chat.get("type") == "private":
        sender = message.get("from") or {}
        user_id = sender.get("id")
        if type(user_id) is not int:
            return "I couldn't identify your Telegram account for the tutor request."
        return await course_tutor_service.answer_general_question(
            database, ai_binding, search_api_key, str(chat_id), user_id, question, now_ms,
            lesson_base_url=lesson_base_url,
        )
    if chat.get("type") not in {"group", "supergroup"} or message.get("message_thread_id") is None:
        return "Ask in the mapped course topic so I can search the correct course materials."
    sender = message.get("from") or {}
    user_id = sender.get("id")
    if type(user_id) is not int:
        return "I couldn't identify your Telegram account for the tutor request."
    if ai_binding is None:
        return "The course tutor is temporarily unavailable. Please try again shortly."
    return await course_tutor_service.answer_course_question(
        database,
        ai_binding,
        search_api_key,
        str(chat_id),
        message.get("message_thread_id"),
        user_id,
        question,
        now_ms,
        lesson_base_url=lesson_base_url,
    )


async def _handle_personal_task_callback_update(database, token, update, now_ms):
    callback = update.get("callback_query")
    if not isinstance(callback, dict) or not str(callback.get("data", "")).startswith("pt:"):
        return None
    callback_id = str(callback.get("id", ""))
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    user_id = (callback.get("from") or {}).get("id")
    if not callback_id:
        return "__QUIZ_HANDLED__"
    if chat.get("type") != "private" or type(user_id) is not int or str(chat.get("id")) != str(user_id):
        await answer_telegram_callback(token, callback_id, "Open the bot privately to manage your tasks.")
        return "__QUIZ_HANDLED__"
    data = str(callback.get("data", ""))
    await answer_telegram_callback(token, callback_id)
    response = "That task action isn't available. Open My tasks and try again."
    reply_markup = None
    if data == "pt:forget:yes":
        deleted = await personal_task_service.delete_all_tasks(database, user_id, now_ms)
        response = f"🧹 Deleted {deleted} personal task{'s' if deleted != 1 else ''} and reminder{'s' if deleted != 1 else ''}."
    elif data == "pt:forget:no":
        response = "Your tasks are still saved."
    else:
        match = re.fullmatch(r"pt:(c|x|d|r|e):(\d{1,16})", data)
        if match:
            action, task_id_text = match.groups()
            task_id = int(task_id_text)
            if action == "c":
                done = await personal_task_service.complete_task(database, user_id, task_id, now_ms)
                response = "✅ Task completed." if done else "I couldn't find that open task in your account."
            elif action == "x":
                cancelled = await personal_task_service.cancel_task(database, user_id, task_id, now_ms)
                response = "Task cancelled." if cancelled else "I couldn't find that open task in your account."
            elif action == "d":
                deleted = await personal_task_service.delete_task(database, user_id, task_id, now_ms)
                response = "Task deleted." if deleted else "I couldn't find that task in your account."
            else:
                if action == "r":
                    intent = personal_task_service.TaskIntent(
                        action="reschedule_task", task_id=task_id, clarification=HOME_RESCHEDULE_PROMPT
                    )
                    response = HOME_RESCHEDULE_PROMPT
                else:
                    intent = personal_task_service.TaskIntent(
                        action="rename_task", task_id=task_id, clarification=HOME_RENAME_PROMPT
                    )
                    response = HOME_RENAME_PROMPT
                await personal_task_service.save_pending_action(
                    database, user_id, int(message.get("message_id") or now_ms), intent, now_ms
                )
                reply_markup = {"force_reply": True, "input_field_placeholder": "Type your answer…"}
    await send_telegram_message(token, {
        "chat_id": str(user_id),
        "message_text": response,
        "reply_markup": reply_markup,
    })
    return "__QUIZ_HANDLED__"


async def _handle_home_callback_update(database, token, update, now_ms):
    callback = update.get("callback_query")
    if not isinstance(callback, dict) or not str(callback.get("data", "")).startswith("home:"):
        return None
    callback_id = str(callback.get("id", ""))
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    action = str(callback.get("data", "")).split(":", 1)[1]
    if not callback_id or not chat_id:
        return "__QUIZ_HANDLED__"
    await answer_telegram_callback(token, callback_id)

    response = None
    reply_markup = None
    thread_id = message.get("message_thread_id")
    if action == "ask":
        response = HOME_ASK_PROMPT
        reply_markup = {
            "force_reply": True,
            "input_field_placeholder": "Type your study question…",
        }
    elif action == "upload":
        response = (
            "<b>Send a file or image to analyze</b>\n"
            "Attach your question as its caption, or send it without a caption for a clear summary.\n\n"
            "I can read PDFs, common images, Word and spreadsheet files up to 20 MB. "
            "Your uploaded content is analyzed for this reply and isn't added to course materials."
        )
    elif action == "memory":
        if chat.get("type") != "private":
            response = "For privacy, view your saved tutor preference in a private chat with me."
        else:
            user_id = (callback.get("from") or {}).get("id")
            try:
                preference = await course_tutor_service.get_tutor_preferences(
                    database, int(user_id)
                ) if type(user_id) is int else ""
                response = (
                    "<b>Your study preference</b>\n" + escape(preference)
                    if preference else "No preference saved yet. Tap <b>Update preference</b> below to add one."
                )
                reply_markup = {
                    "inline_keyboard": [[
                        {"text": "✏️ Update preference", "callback_data": "home:remember"},
                        {"text": "🗑 Forget tutor memory", "callback_data": "home:forget"},
                    ]]
                }
            except Exception:
                response = "I couldn't load your preference right now. Please try again shortly."
    elif action == "remember":
        if chat.get("type") != "private":
            response = "For privacy, update your study preference in a private chat with me."
        else:
            response = HOME_REMEMBER_PROMPT
            reply_markup = {
                "force_reply": True,
                "input_field_placeholder": "Describe how you like to study…",
            }
    elif action == "forget":
        if chat.get("type") != "private":
            response = "For privacy, erase your tutor memory in a private chat with me."
        else:
            user_id = (callback.get("from") or {}).get("id")
            try:
                if type(user_id) is int:
                    await course_tutor_service.forget_tutor_user(database, user_id)
                response = "Your saved tutor conversations and preferences have been erased."
            except Exception:
                response = "I couldn't erase your tutor data right now. Please try again shortly."
    elif action == "upcoming":
        if chat.get("type") not in {"group", "supergroup"}:
            response = "Use Upcoming from your class group to see its assignments and exams."
        else:
            response = await _upcoming_message(database, chat_id, now_ms)
    elif action == "resources":
        if chat.get("type") not in {"group", "supergroup"}:
            response = "Search for shared class files from your class group."
        else:
            response = HOME_RESOURCE_PROMPT
            reply_markup = {
                "force_reply": True,
                "input_field_placeholder": "Type a keyword…",
            }
    elif action in {"tasks", "add_reminder", "timezone", "forget_tasks"}:
        user_id = (callback.get("from") or {}).get("id")
        if chat.get("type") != "private" or type(user_id) is not int:
            response = "For privacy, use personal tasks and reminders in a private chat with me."
        elif action == "tasks":
            task_reply = _personal_task_list_reply(
                await personal_task_service.list_tasks(database, user_id, "open", 10),
                await personal_task_service.get_user_timezone(database, user_id),
            )
            response, reply_markup = task_reply.text, task_reply.reply_markup
        elif action == "add_reminder":
            response = HOME_TASK_PROMPT
            reply_markup = {"force_reply": True, "input_field_placeholder": "Remind me to…"}
        elif action == "timezone":
            timezone_name = await personal_task_service.get_user_timezone(database, user_id)
            response = f"Your current timezone is <code>{escape(timezone_name)}</code>.\n{HOME_TIMEZONE_PROMPT}"
            reply_markup = {"force_reply": True, "input_field_placeholder": "Area/City"}
        else:
            response = "Delete all of your personal tasks and reminders? This cannot be undone."
            reply_markup = {"inline_keyboard": [[
                {"text": "Delete all tasks", "callback_data": "pt:forget:yes"},
                {"text": "Keep them", "callback_data": "pt:forget:no"},
            ]]}
    elif action == "help":
        response = _help_message(chat.get("type", "private"))
    else:
        response = "That action isn't available. Use the Menu button to see what I can do."

    await send_telegram_message(token, {
        "chat_id": chat_id,
        "thread_id": thread_id,
        "message_text": response or "",
        "reply_markup": reply_markup,
    })
    return "__QUIZ_HANDLED__"


async def _handle_quiz_callback_update(database, token, update, now_ms):
    callback = update.get("callback_query")
    if not isinstance(callback, dict) or not str(callback.get("data", "")).startswith("quiz:"):
        return None
    await _process_quiz_callback(database, token, callback, now_ms)
    return "__QUIZ_HANDLED__"


async def _handle_private_quiz_start(database, token, message, chat, text, now_ms):
    if chat.get("type") != "private":
        return None
    match = re.fullmatch(r"\s*/start(?:@\w+)?\s+(quiz_\d{1,12})\s*", text, re.IGNORECASE)
    if not match:
        return None
    result = await _start_private_quiz(database, token, message, match.group(1), now_ms)
    return result or "__QUIZ_HANDLED__"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _load_public_lesson(database, lesson_id: str, now_ms: int) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,48}", lesson_id):
        return None
    row = await database.prepare(
        "SELECT lesson_id, title, article_html, visual_svg, sources_json, expires_at_ms "
        "FROM tutor_visual_lessons WHERE lesson_id = ?"
    ).bind(lesson_id).first()
    if not row:
        return None
    if int(row.get("expires_at_ms", 0)) <= now_ms:
        await database.prepare(
            "DELETE FROM tutor_visual_lessons WHERE lesson_id = ?"
        ).bind(lesson_id).run()
        return None
    return row


@app.get("/lesson/{lesson_id}", response_class=HTMLResponse)
async def visual_lesson_page(lesson_id: str, request: Request):
    runtime_env = request.scope.get("env")
    if not runtime_env or not getattr(runtime_env, "DB", None):
        return HTMLResponse("Lesson not found", status_code=404)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        row = await _load_public_lesson(runtime_env.DB, lesson_id, now_ms)
    except Exception:
        logger.exception("visual_lesson_read_failed")
        return HTMLResponse("Lesson is temporarily unavailable", status_code=503)
    if not row:
        return HTMLResponse("Lesson not found or expired", status_code=404)
    public_base_url = str(getattr(runtime_env, "PUBLIC_BASE_URL", request.base_url)).rstrip("/")
    return HTMLResponse(
        lesson_page_service.render_lesson_page(row, public_base_url),
        headers={
            "Cache-Control": "public, max-age=300",
            "X-Robots-Tag": "noindex, nofollow",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' https:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.get("/lesson/{lesson_id}/visual.svg")
async def visual_lesson_image(lesson_id: str, request: Request):
    runtime_env = request.scope.get("env")
    if not runtime_env or not getattr(runtime_env, "DB", None):
        return Response("Lesson not found", status_code=404)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        row = await _load_public_lesson(runtime_env.DB, lesson_id, now_ms)
    except Exception:
        logger.exception("visual_lesson_image_read_failed")
        return Response("Lesson is temporarily unavailable", status_code=503)
    if not row:
        return Response("Lesson not found or expired", status_code=404)
    return Response(
        str(row["visual_svg"]),
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "public, max-age=300",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        },
    )


async def _read_authenticated_update(
    request: Request,
    expected_secret: str,
    provided_secret: str,
) -> dict | JSONResponse:
    """Validate the Telegram webhook header and bounded JSON body."""
    if not expected_secret:
        return JSONResponse({"status": "not_configured"}, status_code=503)
    if not hmac.compare_digest(provided_secret, expected_secret):
        return JSONResponse({"status": "forbidden"}, status_code=403)

    raw_body = await request.body()
    if len(raw_body) > 256_000:
        return JSONResponse({"status": "payload_too_large"}, status_code=413)
    try:
        update = json.loads(raw_body)
    except (ValueError, TypeError):
        return JSONResponse({"status": "invalid_json"}, status_code=400)
    if not isinstance(update, dict) or type(update.get("update_id")) is not int or update["update_id"] < 0:
        return JSONResponse({"status": "invalid_update"}, status_code=400)

    return update


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> JSONResponse:
    """Authenticate Telegram, claim its update idempotently, and run supported flows."""
    runtime_env = request.scope.get("env")
    expected_secret = getattr(runtime_env, "TELEGRAM_WEBHOOK_SECRET", "") if runtime_env else ""
    update = await _read_authenticated_update(
        request,
        expected_secret,
        x_telegram_bot_api_secret_token,
    )
    if isinstance(update, JSONResponse):
        return update
    if not runtime_env or not getattr(runtime_env, "BOT_TOKEN", ""):
        return JSONResponse({"status": "not_configured"}, status_code=503)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    update_id = update["update_id"]
    typing_task = None
    try:
        claimed = await claim_telegram_update(runtime_env.DB, update_id, now_ms)
        if not claimed:
            return JSONResponse({"status": "duplicate"})
        incoming_message = update.get("message") or {}
        incoming_text = incoming_message.get("text") or incoming_message.get("caption")
        incoming_chat = incoming_message.get("chat") or {}
        has_tutor_attachment = bool(
            incoming_chat.get("type") == "private"
            and (incoming_message.get("photo") or incoming_message.get("document"))
        )
        progressive_draft = None
        private_text = isinstance(incoming_text, str) and incoming_text.strip()
        is_private_tutor_request = bool(
            incoming_chat.get("type") == "private"
            and not (incoming_message.get("from") or {}).get("is_bot")
            and (
                has_tutor_attachment
                or (private_text and not incoming_text.lstrip().startswith("/"))
                or (private_text and re.match(r"^/ask(?:@\w+)?(?:\s|$)", incoming_text, re.IGNORECASE))
            )
        )
        if (isinstance(incoming_text, str) and incoming_text.strip()) or has_tutor_attachment:
            typing_chat_id = str(incoming_chat.get("id", ""))
            typing_thread_id = incoming_message.get("message_thread_id")
            await send_telegram_typing_action(runtime_env.BOT_TOKEN, typing_chat_id, typing_thread_id)
            typing_task = asyncio.create_task(
                _keep_telegram_typing(runtime_env.BOT_TOKEN, typing_chat_id, typing_thread_id)
            )
            ai_binding = getattr(runtime_env, "AI", None)
            if is_private_tutor_request and ai_binding is not None:
                progressive_draft = TelegramTutorDraft(
                    runtime_env.BOT_TOKEN,
                    typing_chat_id,
                    int(incoming_message.get("message_id") or update_id or 1),
                    typing_thread_id,
                )
                if await progressive_draft.start():
                    ai_binding = ProgressiveTutorBinding(ai_binding, progressive_draft.on_partial)
        else:
            ai_binding = getattr(runtime_env, "AI", None)
        try:
            reply = await _process_telegram_update(
                runtime_env.DB,
                runtime_env.BOT_TOKEN,
                update_id,
                update,
                now_ms,
                getattr(runtime_env, "ACADEMIC_GROQ_API_KEY", "")
                or getattr(runtime_env, "GROQ_API_KEY", ""),
                getattr(runtime_env, "ACADEMIC_EXTRACTION_MODEL", "llama-3.1-8b-instant"),
                getattr(runtime_env, "BOT_USERNAME", ""),
                ai_binding,
                getattr(runtime_env, "TAVILY_SEARCH_API_KEY", ""),
                getattr(runtime_env, "PUBLIC_BASE_URL", ""),
                getattr(runtime_env, "TELEGRAM_IV_RHASH", ""),
            )
            tutor_reply_markup = None
            message = update.get("message") or {}
            if (
                is_private_tutor_request
                and isinstance(reply, (str, course_tutor_service.TutorReply))
                and reply != "__QUIZ_HANDLED__"
            ):
                incoming_question = message.get("text") or message.get("caption") or ""
                if isinstance(incoming_question, str) and re.match(r"^/ask(?:@\w+)?(?:\s|$)", incoming_question, re.IGNORECASE):
                    incoming_question = re.sub(r"^/ask(?:@\w+)?\s*", "", incoming_question, flags=re.IGNORECASE)
                reply, tutor_reply_markup = await _tutor_buttons(
                    runtime_env.DB, ai_binding,
                    (message.get("from") or {}).get("id"),
                    str((message.get("chat") or {}).get("id", "")),
                    incoming_question, reply, now_ms,
                    str(getattr(runtime_env, "TELEGRAM_IV_RHASH", "") or ""),
                )
        finally:
            if progressive_draft is not None:
                await progressive_draft.finish()
            if typing_task is not None:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
        message = update.get("message") or {}
        if isinstance(reply, (PersonalTaskReply, TelegramReply)):
            tutor_reply_markup = reply.reply_markup
            reply = reply.text
        if isinstance(reply, course_tutor_service.TutorReply):
            lesson_url = _instant_view_url(
                reply.lesson_url,
                str(getattr(runtime_env, "TELEGRAM_IV_RHASH", "") or ""),
            )
            tutor_reply_markup = {
                "inline_keyboard": [[{"text": "📖 Open visual lesson", "url": lesson_url}]]
            }
            reply = reply.text
        if reply and reply != "__QUIZ_HANDLED__":
            chat_id = str((message.get("chat") or {}).get("id", ""))
            incoming_command = message.get("text") or ""
            reply_markup = tutor_reply_markup
            if re.match(r"^/(?:start|help)(?:@\w+)?(?:\s|$)", incoming_command, re.IGNORECASE):
                reply_markup = _welcome_reply_markup((message.get("chat") or {}).get("type", "private"))
            await send_telegram_message(
                runtime_env.BOT_TOKEN,
                {
                    "chat_id": chat_id,
                    "thread_id": message.get("message_thread_id"),
                    "message_text": reply,
                    "reply_markup": reply_markup,
                    "disable_link_preview": (message.get("chat") or {}).get("type") == "private",
                },
            )
        status = "processed" if reply else "ignored"
        await finish_telegram_update(runtime_env.DB, update_id, status, now_ms)
        return JSONResponse({"status": status})
    except Exception:
        logger.exception("telegram_update_processing_failed update_id=%s", update_id)
        try:
            await finish_telegram_update(
                runtime_env.DB,
                update_id,
                "failed",
                now_ms,
                "update_processing_failed",
            )
        except Exception:
            logger.exception("telegram_update_receipt_failed update_id=%s", update_id)
        return JSONResponse({"status": "retry"}, status_code=503)


@app.post("/internal/academic-items")
async def sync_items(
    payload: AcademicItemSyncBatch,
    request: Request,
    x_academic_sync_secret: str = Header(default=""),
) -> JSONResponse:
    """Receive bounded, authenticated academic record updates from the bot."""
    runtime_env = request.scope.get("env")
    expected = getattr(runtime_env, "ACADEMIC_SYNC_SECRET", "") if runtime_env else ""
    if not expected or not hmac.compare_digest(x_academic_sync_secret, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        count = await sync_academic_items(runtime_env.DB, payload.items)
    except Exception:
        logger.exception("academic_item_sync_failed")
        return JSONResponse({"status": "not_synced"}, status_code=503)
    return JSONResponse({"status": "synced", "items": count})


@app.post("/internal/colleges")
async def provision_college(
    payload: CollegeProvision,
    request: Request,
    x_academic_sync_secret: str = Header(default=""),
) -> JSONResponse:
    """Provision one college and a hashed, use-limited group setup code."""
    runtime_env = request.scope.get("env")
    expected = getattr(runtime_env, "ACADEMIC_SYNC_SECRET", "") if runtime_env else ""
    if not expected or not hmac.compare_digest(x_academic_sync_secret, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        await runtime_env.DB.prepare(
            """
            INSERT INTO colleges (college_id, college_name, status, created_at_ms)
            VALUES (?, ?, 'active', ?)
            ON CONFLICT(college_id) DO UPDATE SET college_name = excluded.college_name, status = 'active'
            """
        ).bind(payload.college_id, payload.college_name.strip(), now_ms).run()
        await runtime_env.DB.prepare(
            """
            INSERT INTO college_invite_codes (
                code_hash, college_id, remaining_uses, active, expires_at_ms, created_at_ms
            ) VALUES (?, ?, ?, 1, NULL, ?)
            ON CONFLICT(code_hash) DO UPDATE SET
                college_id = excluded.college_id,
                remaining_uses = excluded.remaining_uses,
                active = 1
            """
        ).bind(
            _invite_code_hash(payload.invite_code),
            payload.college_id,
            payload.remaining_uses,
            now_ms,
        ).run()
    except Exception:
        logger.exception("college_provision_failed college_id=%s", payload.college_id)
        return JSONResponse({"status": "not_provisioned"}, status_code=503)
    return JSONResponse({"status": "provisioned", "college_id": payload.college_id})


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    try:
        result = await request.scope["env"].DB.prepare(
            "SELECT cron_runs, claimed_reminders FROM runtime_probe WHERE id = ?"
        ).bind(1).first()
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return JSONResponse(
        {
            "status": "ready",
            "cron_runs": result["cron_runs"],
            "claimed_reminders": result["claimed_reminders"],
        }
    )


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        import asgi

        return await asgi.fetch(app, request.js_object, self.env)

    async def scheduled(self, controller, env, ctx):
        runtime_env = env if env is not None else self.env
        if getattr(controller, "cron", "") == "0 0 * * *":
            await runtime_env.DB.prepare(
                "DELETE FROM tutor_visual_lessons WHERE expires_at_ms <= ?"
            ).bind(controller.scheduledTime).run()
            expiry_cutoff = int(controller.scheduledTime) - 24 * 60 * 60 * 1000
            await runtime_env.DB.prepare(
                "DELETE FROM tutor_documents WHERE created_at_ms <= ?"
            ).bind(expiry_cutoff).run()
            await runtime_env.DB.prepare(
                "DELETE FROM tutor_followups WHERE created_at_ms <= ?"
            ).bind(expiry_cutoff).run()
            await runtime_env.DB.prepare(
                "DELETE FROM tutor_followup_sets WHERE created_at_ms <= ?"
            ).bind(expiry_cutoff).run()
        await runtime_env.DB.prepare(
            "UPDATE runtime_probe SET last_cron_at = ?, cron_runs = cron_runs + 1 WHERE id = ?"
        ).bind(controller.scheduledTime, 1).run()
        claimed = await claim_due_reminders(runtime_env.DB, controller.scheduledTime)
        await dispatch_claimed_reminders(
            runtime_env.DB,
            claimed,
            lambda reminder: send_telegram_message(runtime_env.BOT_TOKEN, reminder),
            controller.scheduledTime,
        )
        if claimed:
            await runtime_env.DB.prepare(
                "UPDATE runtime_probe SET claimed_reminders = claimed_reminders + ? WHERE id = ?"
            ).bind(len(claimed), 1).run()
        try:
            personal_claimed = await personal_task_service.claim_due_personal_task_reminders(
                runtime_env.DB, controller.scheduledTime
            )
            await personal_task_service.dispatch_claimed_personal_task_reminders(
                runtime_env.DB,
                personal_claimed,
                lambda reminder: _send_personal_task_reminder(runtime_env.BOT_TOKEN, reminder),
                controller.scheduledTime,
            )
            if personal_claimed:
                await runtime_env.DB.prepare(
                    "UPDATE runtime_probe SET claimed_reminders = claimed_reminders + ? WHERE id = ?"
                ).bind(len(personal_claimed), 1).run()
        except Exception:
            logger.exception("personal_task_reminder_tick_failed")
        await enqueue_weekly_digests(runtime_env.DB, controller.scheduledTime)
        digest_batch = await claim_due_weekly_digests(runtime_env.DB, controller.scheduledTime)
        await dispatch_claimed_weekly_digests(
            runtime_env.DB,
            digest_batch,
            lambda digest: send_telegram_message(runtime_env.BOT_TOKEN, digest),
            controller.scheduledTime,
        )
        if getattr(runtime_env, "BOT_TOKEN", "") and getattr(runtime_env, "AI", None):
            await course_quiz_service.process_quiz_tick(
                runtime_env.DB,
                runtime_env.BOT_TOKEN,
                runtime_env.AI,
                controller.scheduledTime,
                send_telegram_message,
                send_telegram_poll,
                process_index=course_tutor_service.process_next_index_job,
            )
