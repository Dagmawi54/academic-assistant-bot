"""Durable, owner-scoped personal tasks and one-time reminders."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TASK_INTENT_MODEL = "@cf/zai-org/glm-4.7-flash"
DEFAULT_TIMEZONE = "Africa/Addis_Ababa"
MAX_TASK_TITLE_CHARS = 300
MAX_TASKS_PER_PAGE = 20
REMINDER_BATCH_SIZE = 5
REMINDER_LEASE_MS = 60_000
REMINDER_MAX_ATTEMPTS = 5
REMINDER_RETRY_BASE_MS = 30_000
REMINDER_RETRY_MAX_MS = 3_600_000


@dataclass(frozen=True)
class TaskIntent:
    action: str
    title: str | None = None
    due_at_ms: int | None = None
    reminder_at_ms: int | None = None
    task_id: int | None = None
    clarification: str | None = None


class DefiniteDeliveryError(Exception):
    """Telegram definitively rejected a reminder message; retry is safe."""


def get_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("unsupported_timezone") from None


def looks_like_task_request(text: str) -> bool:
    text = str(text or "")[:2000]
    return bool(re.search(
        r"\bremind\s+me\b|\bmy\s+tasks?\b|"
        r"\b(?:add|save|create|make|set)\s+(?:(?:this|that|a|an)\s+)?"
        r"(?:to\s+my\s+)?(?:tasks?|reminders?)\b|"
        r"\b(?:mark|complete|finish|cancel|delete|reschedule|rename)\s+(?:my\s+)?(?:task|reminder)\b",
        text,
        re.IGNORECASE,
    ))


async def get_user_timezone(database, user_id: int) -> str:
    row = await database.prepare(
        "SELECT timezone_name FROM assistant_user_settings WHERE user_id = ?"
    ).bind(user_id).first()
    return str((row or {}).get("timezone_name") or DEFAULT_TIMEZONE)


async def set_user_timezone(database, user_id: int, timezone_name: str, now_ms: int) -> bool:
    if type(user_id) is not int or user_id <= 0:
        return False
    timezone_name = str(timezone_name or "").strip()
    try:
        get_timezone(timezone_name)
    except ValueError:
        return False
    row = await database.prepare(
        """
        INSERT INTO assistant_user_settings (user_id, timezone_name, updated_at_ms)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            timezone_name = excluded.timezone_name,
            updated_at_ms = excluded.updated_at_ms
        RETURNING user_id
        """
    ).bind(user_id, timezone_name, now_ms).first()
    return bool(row)


def _clarify(question: str = "What date and time should I use for that reminder?") -> TaskIntent:
    question = re.sub(r"\s+", " ", str(question or "")).strip()
    if not question or len(question) > 220:
        question = "What date and time should I use for that reminder?"
    return TaskIntent(action="clarification", clarification=question)


def _parse_relative_reminder(text: str, timezone_name: str, now_ms: int) -> TaskIntent | None:
    """Safely handle explicit short relative reminders without a model round-trip."""
    match = re.search(r"\b(?:in|after)\s+(\d{1,4})\s*(minutes?|mins?|hours?|hrs?|days?)\b", text, re.IGNORECASE)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).casefold()
    multiplier = 60_000 if unit.startswith(("min",)) else 3_600_000 if unit.startswith("h") else 86_400_000
    delta = amount * multiplier
    if amount <= 0 or delta > 365 * 86_400_000:
        return None
    try:
        get_timezone(timezone_name)
    except ValueError:
        timezone_name = DEFAULT_TIMEZONE
    remainder = text[:match.start()] + " " + text[match.end():]
    remainder = re.sub(r"\b(?:can|could|would)\s+(?:you|u)\s+", " ", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"\b(?:please|put|set|add|create|make|a|an|the|personal|task|tasks|reminder|reminders|for|me|to|remind)\b", " ", remainder, flags=re.IGNORECASE)
    title = re.sub(r"[^\w\s'-]", " ", remainder, flags=re.UNICODE)
    title = re.sub(r"\s+", " ", title).strip(" -'\t\r\n")[:MAX_TASK_TITLE_CHARS]
    if title.casefold() in {"", "u", "you", "my", "it", "this", "that"}:
        title = "Reminder"
    return TaskIntent(action="create_task", title=title, reminder_at_ms=int(now_ms) + delta)


def _parse_iso_in_zone(value, zone: ZoneInfo, now_ms: int, *, is_reminder: bool) -> int | None:
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    local_naive = parsed.replace(tzinfo=None)
    fold_zero = local_naive.replace(tzinfo=zone, fold=0)
    fold_one = local_naive.replace(tzinfo=zone, fold=1)
    if fold_zero.utcoffset() != fold_one.utcoffset():
        return None
    roundtrip = parsed.astimezone(zone)
    if roundtrip.replace(tzinfo=None) != local_naive:
        return None
    if parsed.utcoffset() != roundtrip.utcoffset():
        return None
    instant = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    if is_reminder and instant <= now_ms:
        return None
    return instant


def validate_task_intent(raw, timezone_name: str, now_ms: int) -> TaskIntent:
    """Validate model-proposed intent and convert explicit zoned instants to UTC milliseconds."""
    if not isinstance(raw, dict):
        return _clarify()
    action = raw.get("action")
    supported = {
        "create_task", "list_tasks", "complete_task", "cancel_task", "reschedule_task",
        "rename_task", "delete_task", "delete_all_tasks", "clarification", "not_task_request",
    }
    if action not in supported:
        return _clarify()
    if action == "not_task_request":
        return TaskIntent(action=action)
    if action == "clarification":
        return _clarify(raw.get("clarifying_question"))
    if action == "list_tasks" or action == "delete_all_tasks":
        return TaskIntent(action=action)
    if action in {"complete_task", "cancel_task", "delete_task", "reschedule_task"}:
        try:
            task_id = int(raw.get("task_id"))
        except (TypeError, ValueError):
            return _clarify("Which task do you mean? Open My tasks and choose it there.")
        if task_id <= 0:
            return _clarify("Which task do you mean? Open My tasks and choose it there.")
        if action != "reschedule_task":
            return TaskIntent(action=action, task_id=task_id)
    else:
        task_id = None

    title = str(raw.get("title") or "").strip()
    if action == "create_task" and not 1 <= len(title) <= MAX_TASK_TITLE_CHARS:
        return _clarify("What should I call this task?")
    try:
        zone = get_timezone(timezone_name)
    except ValueError:
        zone = get_timezone(DEFAULT_TIMEZONE)

    due_at_ms = None
    reminder_at_ms = None
    due_value = raw.get("due_local")
    reminder_value = raw.get("reminder_local")
    if due_value is not None:
        due_at_ms = _parse_iso_in_zone(due_value, zone, now_ms, is_reminder=False)
        if due_at_ms is None:
            return _clarify("I couldn't verify that deadline in your timezone. What date and time did you mean?")
    if reminder_value is not None:
        reminder_at_ms = _parse_iso_in_zone(reminder_value, zone, now_ms, is_reminder=True)
        if reminder_at_ms is None:
            return _clarify("That reminder time is past, ambiguous, or doesn't match your timezone. What date and time should I use?")
    if action == "create_task" and due_at_ms is None and reminder_at_ms is None:
        return _clarify()
    if action == "reschedule_task" and reminder_at_ms is None:
        return _clarify("What new date and time should I use?")
    if due_at_ms is not None and reminder_at_ms is not None and reminder_at_ms > due_at_ms:
        return _clarify("Should the reminder be before the deadline? What time should I use?")
    return TaskIntent(
        action=action,
        title=title or None,
        due_at_ms=due_at_ms,
        reminder_at_ms=reminder_at_ms,
        task_id=task_id,
    )


def _extract_json_result(result):
    if isinstance(result, dict):
        if isinstance(result.get("response"), str):
            result = result["response"]
        elif isinstance(result.get("choices"), list) and result["choices"]:
            message = result["choices"][0].get("message") or {}
            result = message.get("content")
    if not isinstance(result, str):
        return result if isinstance(result, dict) else None
    text = result.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


async def parse_task_intent(ai_binding, text: str, timezone_name: str, now_ms: int) -> TaskIntent:
    """Ask the existing Workers AI binding to classify a possible private task request."""
    if ai_binding is None or not isinstance(text, str) or not text.strip():
        return TaskIntent(action="not_task_request")
    relative = _parse_relative_reminder(text, timezone_name, now_ms)
    if relative is not None and re.search(r"\b(?:remind|reminder)\b", text, re.IGNORECASE):
        return relative
    try:
        zone = get_timezone(timezone_name)
    except ValueError:
        zone = get_timezone(DEFAULT_TIMEZONE)
        timezone_name = DEFAULT_TIMEZONE
    local_now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(zone).isoformat()
    messages = [
        {
            "role": "system",
            "content": (
                "Classify whether the user is managing personal tasks or one-time reminders. "
                "Return one JSON object only. Actions: create_task, list_tasks, complete_task, "
                "cancel_task, reschedule_task, delete_task, delete_all_tasks, clarification, "
                "not_task_request. Never infer an unprovided task ID; use clarification when a "
                "specific task cannot be identified. For date-times, return ISO 8601 timestamps "
                "with the correct numeric UTC offset for the user's IANA timezone. Include title, "
                "due_local, reminder_local, task_id, and clarifying_question only when applicable. "
                "If the user asks an ordinary study question, return not_task_request. If a reminder "
                "date/time or action is unclear, return clarification and ask one short question. "
                f"Timezone: {timezone_name}. Current local time: {local_now}."
            ),
        },
        {"role": "user", "content": text[:2000]},
    ]
    try:
        result = await ai_binding.run(
            TASK_INTENT_MODEL,
            {
                "messages": messages,
                "temperature": 0,
                "max_tokens": 360,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
    except Exception:
        return _clarify("I couldn't check that task request just now. Please try again.")
    raw = _extract_json_result(result)
    if raw is None:
        return _clarify("I couldn't safely read the date or task. Could you rephrase it?")
    return validate_task_intent(raw, timezone_name, now_ms)


def _stable_task_id(user_id: int, source_update_id: int | None, now_ms: int) -> int:
    source = str(source_update_id) if source_update_id is not None else f"{now_ms}:{user_id}"
    digest = hashlib.blake2b(f"{user_id}:{source}".encode(), digest_size=7).digest()
    return int.from_bytes(digest, "big") or 1


async def create_task(database, user_id: int, source_update_id: int | None, intent: TaskIntent, timezone_name: str, now_ms: int) -> dict | None:
    if type(user_id) is not int or user_id <= 0 or intent.action != "create_task" or not intent.title:
        return None
    task_id = _stable_task_id(user_id, source_update_id, now_ms)
    statements = [database.prepare(
        """
        INSERT OR IGNORE INTO personal_tasks
            (id, user_id, source_update_id, title, due_at_ms, status, created_at_ms, updated_at_ms)
        VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
        """
    ).bind(task_id, user_id, source_update_id, intent.title, intent.due_at_ms, now_ms, now_ms)]
    if intent.reminder_at_ms is not None:
        statements.append(database.prepare(
            """
            INSERT OR IGNORE INTO personal_task_reminders
                (task_id, user_id, scheduled_at_ms, message_text, status, attempt_count, created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
            """
        ).bind(task_id, user_id, intent.reminder_at_ms, intent.title, now_ms, now_ms))
    await database.batch(statements)
    return await database.prepare(
        "SELECT id, user_id, title, due_at_ms, status, created_at_ms FROM personal_tasks WHERE id = ? AND user_id = ?"
    ).bind(task_id, user_id).first()


async def list_tasks(database, user_id: int, status: str = "open", limit: int = 10, offset: int = 0) -> list[dict]:
    if status not in {"open", "completed", "cancelled", "all"}:
        status = "open"
    limit = min(MAX_TASKS_PER_PAGE, max(1, int(limit)))
    offset = max(0, int(offset))
    if status == "all":
        result = await database.prepare(
            "SELECT id, title, due_at_ms, status FROM personal_tasks WHERE user_id = ? ORDER BY status, due_at_ms, id LIMIT ? OFFSET ?"
        ).bind(user_id, limit, offset).all()
    else:
        result = await database.prepare(
            "SELECT id, title, due_at_ms, status FROM personal_tasks WHERE user_id = ? AND status = ? ORDER BY due_at_ms, id LIMIT ? OFFSET ?"
        ).bind(user_id, status, limit, offset).all()
    return list((result or {}).get("results") or [])


async def _task_mutation(database, user_id: int, task_id: int, query: str, values: tuple) -> bool:
    result = await database.prepare(query).bind(*values, task_id, user_id).first()
    return bool(result)


async def complete_task(database, user_id: int, task_id: int, now_ms: int) -> bool:
    statements = [
        database.prepare(
            "UPDATE personal_tasks SET status = 'completed', completed_at_ms = ?, updated_at_ms = ? WHERE id = ? AND user_id = ? AND status = 'open'"
        ).bind(now_ms, now_ms, task_id, user_id),
        database.prepare(
            "UPDATE personal_task_reminders SET status = 'cancelled', lease_until_ms = NULL, updated_at_ms = ? WHERE task_id = ? AND user_id = ? AND status IN ('pending', 'failed', 'sending')"
        ).bind(now_ms, task_id, user_id),
    ]
    await database.batch(statements)
    row = await database.prepare(
        "SELECT status FROM personal_tasks WHERE id = ? AND user_id = ?"
    ).bind(task_id, user_id).first()
    return bool(row and row.get("status") == "completed")


async def cancel_task(database, user_id: int, task_id: int, now_ms: int) -> bool:
    await database.batch([
        database.prepare(
            "UPDATE personal_tasks SET status = 'cancelled', updated_at_ms = ? WHERE id = ? AND user_id = ? AND status = 'open'"
        ).bind(now_ms, task_id, user_id),
        database.prepare(
            "UPDATE personal_task_reminders SET status = 'cancelled', lease_until_ms = NULL, updated_at_ms = ? WHERE task_id = ? AND user_id = ? AND status IN ('pending', 'failed', 'sending')"
        ).bind(now_ms, task_id, user_id),
    ])
    row = await database.prepare(
        "SELECT status FROM personal_tasks WHERE id = ? AND user_id = ?"
    ).bind(task_id, user_id).first()
    return bool(row and row.get("status") == "cancelled")


async def reschedule_task(database, user_id: int, task_id: int, new_time_ms: int, now_ms: int) -> bool:
    task = await database.prepare(
        "SELECT title FROM personal_tasks WHERE id = ? AND user_id = ? AND status = 'open'"
    ).bind(task_id, user_id).first()
    if not task or new_time_ms <= now_ms:
        return False
    await database.batch([
        database.prepare(
            "UPDATE personal_tasks SET due_at_ms = ?, updated_at_ms = ? WHERE id = ? AND user_id = ? AND status = 'open'"
        ).bind(new_time_ms, now_ms, task_id, user_id),
        database.prepare(
            """
            INSERT INTO personal_task_reminders
                (task_id, user_id, scheduled_at_ms, message_text, status, attempt_count, created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                scheduled_at_ms = excluded.scheduled_at_ms,
                message_text = excluded.message_text,
                status = 'pending', attempt_count = 0,
                lease_until_ms = NULL, retry_after_ms = NULL,
                sent_at_ms = NULL, last_error = NULL, updated_at_ms = excluded.updated_at_ms
            """
        ).bind(task_id, user_id, new_time_ms, task["title"], now_ms, now_ms),
    ])
    return True


async def rename_task(database, user_id: int, task_id: int, title: str, now_ms: int) -> bool:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    if not 1 <= len(title) <= MAX_TASK_TITLE_CHARS:
        return False
    row = await database.prepare(
        "SELECT id FROM personal_tasks WHERE id = ? AND user_id = ? AND status = 'open'"
    ).bind(task_id, user_id).first()
    if not row:
        return False
    await database.batch([
        database.prepare(
            "UPDATE personal_tasks SET title = ?, updated_at_ms = ? WHERE id = ? AND user_id = ? AND status = 'open'"
        ).bind(title, now_ms, task_id, user_id),
        database.prepare(
            "UPDATE personal_task_reminders SET message_text = ?, updated_at_ms = ? WHERE task_id = ? AND user_id = ? AND status IN ('pending', 'failed')"
        ).bind(title, now_ms, task_id, user_id),
    ])
    return True


async def delete_task(database, user_id: int, task_id: int, now_ms: int) -> bool:
    deleted = await database.prepare(
        "DELETE FROM personal_tasks WHERE id = ? AND user_id = ? RETURNING id"
    ).bind(task_id, user_id).first()
    return bool(deleted)


async def delete_all_tasks(database, user_id: int, now_ms: int) -> int:
    deleted = await database.prepare(
        "DELETE FROM personal_tasks WHERE user_id = ?"
    ).bind(user_id).run()
    await database.prepare(
        "DELETE FROM assistant_pending_actions WHERE user_id = ?"
    ).bind(user_id).run()
    return int((deleted or {}).get("meta", {}).get("changes", 0))


async def save_pending_action(
    database, user_id: int, source_update_id: int, intent: TaskIntent, now_ms: int,
    original_text: str | None = None,
) -> None:
    payload = json.dumps({
        "action": intent.action,
        "title": intent.title,
        "due_at_ms": intent.due_at_ms,
        "reminder_at_ms": intent.reminder_at_ms,
        "task_id": intent.task_id,
        "clarification": intent.clarification,
        "original_text": str(original_text or "")[:2000],
    }, separators=(",", ":"))
    await database.prepare(
        """
        INSERT INTO assistant_pending_actions (user_id, source_update_id, intent_json, expires_at_ms, created_at_ms)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            source_update_id = excluded.source_update_id,
            intent_json = excluded.intent_json,
            expires_at_ms = excluded.expires_at_ms,
            created_at_ms = excluded.created_at_ms
        """
    ).bind(user_id, source_update_id, payload, now_ms + 15 * 60_000, now_ms).run()


async def peek_pending_action(database, user_id: int, now_ms: int) -> dict | None:
    row = await database.prepare(
        "SELECT intent_json FROM assistant_pending_actions WHERE user_id = ? AND expires_at_ms > ?"
    ).bind(user_id, now_ms).first()
    if not row:
        return None
    try:
        value = json.loads(row.get("intent_json") or "{}")
        return value if isinstance(value, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


async def consume_pending_action(database, user_id: int, now_ms: int) -> dict | None:
    row = await database.prepare(
        "DELETE FROM assistant_pending_actions WHERE user_id = ? AND expires_at_ms > ? RETURNING intent_json"
    ).bind(user_id, now_ms).first()
    await database.prepare(
        "DELETE FROM assistant_pending_actions WHERE user_id = ?"
    ).bind(user_id).run()
    if not row:
        return None
    try:
        value = json.loads(row.get("intent_json") or "{}")
        return value if isinstance(value, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


async def claim_due_personal_task_reminders(database, now_ms: int, batch_size: int = REMINDER_BATCH_SIZE) -> list[dict]:
    await database.prepare(
        "UPDATE personal_task_reminders SET status = 'uncertain', lease_until_ms = NULL, last_error = 'delivery_outcome_unknown', updated_at_ms = ? WHERE status = 'sending' AND lease_until_ms <= ?"
    ).bind(now_ms, now_ms).run()
    await database.prepare(
        "UPDATE personal_task_reminders SET status = 'failed', lease_until_ms = NULL, retry_after_ms = NULL, last_error = 'attempt_limit_reached', updated_at_ms = ? WHERE status = 'failed' AND attempt_count >= ?"
    ).bind(now_ms, REMINDER_MAX_ATTEMPTS).run()
    result = await database.prepare(
        """
        UPDATE personal_task_reminders
        SET status = 'sending', attempt_count = attempt_count + 1,
            lease_until_ms = ?, updated_at_ms = ?
        WHERE id IN (
            SELECT r.id FROM personal_task_reminders r
            JOIN personal_tasks t ON t.id = r.task_id AND t.user_id = r.user_id
            WHERE t.status = 'open'
              AND r.status IN ('pending', 'failed')
              AND r.scheduled_at_ms <= ?
              AND (r.retry_after_ms IS NULL OR r.retry_after_ms <= ?)
              AND r.attempt_count < ?
            ORDER BY r.scheduled_at_ms, r.id
            LIMIT ?
        )
        RETURNING id, task_id, user_id, message_text, attempt_count
        """
    ).bind(now_ms + REMINDER_LEASE_MS, now_ms, now_ms, now_ms, REMINDER_MAX_ATTEMPTS, max(1, min(20, batch_size))).all()
    return list((result or {}).get("results") or [])


async def dispatch_claimed_personal_task_reminders(database, reminders: list[dict], send_message, now_ms: int) -> None:
    for reminder in reminders:
        try:
            await send_message({
                "chat_id": str(reminder["user_id"]),
                "message_text": str(reminder["message_text"]),
            })
        except DefiniteDeliveryError:
            attempt = int(reminder.get("attempt_count") or 1)
            retry_ms = min(REMINDER_RETRY_BASE_MS * (2 ** (attempt - 1)), REMINDER_RETRY_MAX_MS)
            retry_after_ms = now_ms + retry_ms if attempt < REMINDER_MAX_ATTEMPTS else None
            await database.prepare(
                "UPDATE personal_task_reminders SET status = 'failed', retry_after_ms = ?, lease_until_ms = NULL, last_error = 'telegram_rejected', updated_at_ms = ? WHERE id = ? AND status = 'sending'"
            ).bind(retry_after_ms, now_ms, reminder["id"]).run()
        except Exception:
            await database.prepare(
                "UPDATE personal_task_reminders SET status = 'uncertain', lease_until_ms = NULL, last_error = 'delivery_outcome_unknown', updated_at_ms = ? WHERE id = ? AND status = 'sending'"
            ).bind(now_ms, reminder["id"]).run()
        else:
            await database.prepare(
                "UPDATE personal_task_reminders SET status = 'sent', sent_at_ms = ?, lease_until_ms = NULL, retry_after_ms = NULL, last_error = NULL, updated_at_ms = ? WHERE id = ? AND status = 'sending'"
            ).bind(now_ms, now_ms, reminder["id"]).run()
