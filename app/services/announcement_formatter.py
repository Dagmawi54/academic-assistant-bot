"""Formatting helpers for clean Telegram announcement delivery."""

from __future__ import annotations

import html
import re

from app.utils.text import sanitize_telegram_html

_URGENT_WORDS = ("urgent", "important", "deadline", "exam", "assignment", "tomorrow", "today")
_DATE_PATTERN = re.compile(
    r"\b("
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"today|tomorrow|tonight|"
    r"\d{1,2}[:.]\d{2}\s*(?:am|pm)?|"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
    r")\b",
    re.IGNORECASE,
)


def _highlight_dates(text: str) -> str:
    return _DATE_PATTERN.sub(lambda m: f"<b>{html.escape(m.group(0))}</b>", text)


def format_announcement_html(text: str) -> str:
    """Convert plain admin text into clean Telegram HTML."""
    normalized = "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())
    if not normalized:
        return "<b>Announcement</b>"

    title = "Announcement"
    lower = normalized.lower()
    if any(word in lower for word in ("exam", "quiz", "mid", "final")):
        title = "Exam Update"
    elif any(word in lower for word in ("assignment", "homework", "project", "submission")):
        title = "Assignment Update"

    icon = "📢"
    if any(word in lower for word in _URGENT_WORDS):
        icon = "⚠️"

    lines = []
    for raw_line in normalized.splitlines():
        line = html.escape(raw_line)
        line = _highlight_dates(line)
        if raw_line.startswith(("-", "*", "•")):
            line = f"• {line.lstrip('-*• ').strip()}"
        lines.append(line)

    body = "\n".join(lines)
    formatted = f"<b>{icon} {title}</b>\n\n{body}"
    return sanitize_telegram_html(formatted)
