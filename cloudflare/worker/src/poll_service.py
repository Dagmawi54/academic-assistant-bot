"""Parsing and validation for anonymous class polls."""

import re

POLL_MAX_OPTIONS = 10


def parse_class_poll_command(text: str) -> dict | None:
    """Parse `/class_poll Question | Option A | Option B [| ...]`."""
    parts = [part.strip() for part in text.split("|")]
    command = re.fullmatch(r"/class_poll(?:@\w+)?\s+(.+)", parts[0], re.IGNORECASE)
    if not command or not 3 <= len(parts) <= POLL_MAX_OPTIONS + 1:
        return None
    question = re.sub(r"\s+", " ", command.group(1)).strip()
    options = [re.sub(r"\s+", " ", option).strip() for option in parts[1:]]
    if (
        not 1 <= len(question) <= 300
        or any(not option or len(option) > 100 for option in options)
        or len({option.casefold() for option in options}) != len(options)
    ):
        return None
    return {"question": question, "options": options}


async def claim_class_poll(database, update_id: int, chat_id: str, now_ms: int) -> bool:
    """Claim a poll command once; uncertain Telegram sends are never replayed."""
    row = await database.prepare(
        "INSERT INTO class_poll_receipts (update_id, chat_id, status, created_at_ms) VALUES (?, ?, 'sending', ?) ON CONFLICT(update_id) DO NOTHING RETURNING update_id"
    ).bind(update_id, chat_id, now_ms).first()
    return bool(row)


async def finish_class_poll(database, update_id: int, status: str, now_ms: int,
                            poll_id: str | None = None, error: str | None = None) -> None:
    """Record a terminal result without allowing an ambiguous poll send to duplicate."""
    await database.prepare(
        "UPDATE class_poll_receipts SET status = ?, poll_id = ?, completed_at_ms = ?, last_error = ? WHERE update_id = ? AND status = 'sending'"
    ).bind(status, poll_id, now_ms, error, update_id).run()
