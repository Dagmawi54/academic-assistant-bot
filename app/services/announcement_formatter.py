"""Formatting helpers for clean Telegram announcement delivery."""

from __future__ import annotations

import html
import re

from app.utils.text import sanitize_telegram_html

_DATE_PATTERN = re.compile(
    r"\b("
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"today|tomorrow|tonight|"
    r"\d{1,2}[:.]\d{2}\s*(?:am|pm)?|"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
    r")\b",
    re.IGNORECASE,
)

_GRAMMAR_REPLACEMENTS = (
    (re.compile(r"\bdont\b", re.IGNORECASE), "don't"),
    (re.compile(r"\bcant\b", re.IGNORECASE), "can't"),
    (re.compile(r"\bwont\b", re.IGNORECASE), "won't"),
    (re.compile(r"\bisnt\b", re.IGNORECASE), "isn't"),
    (re.compile(r"\barent\b", re.IGNORECASE), "aren't"),
    (re.compile(r"\bdidnt\b", re.IGNORECASE), "didn't"),
    (re.compile(r"\bdoesnt\b", re.IGNORECASE), "doesn't"),
    (re.compile(r"\btmrw\b", re.IGNORECASE), "tomorrow"),
)


def _highlight_dates(text: str) -> str:
    return _DATE_PATTERN.sub(lambda match: f"<b>{html.escape(match.group(0))}</b>", text)


def _polish_line(text: str) -> str:
    line = re.sub(r"\s+", " ", text.strip())
    for pattern, replacement in _GRAMMAR_REPLACEMENTS:
        line = pattern.sub(replacement, line)
    line = re.sub(r"\bi\b", "I", line)
    line = re.sub(r"\s+([,.;:!?])", r"\1", line)
    if line:
        line = line[0].upper() + line[1:]
    if line and line[-1] not in ".!?":
        line += "."
    return line


def format_announcement_html(text: str) -> str:
    """Lightly polish plain admin text into natural Telegram HTML."""
    normalized = "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())
    if not normalized:
        return "Announcement."

    lines = []
    for raw_line in normalized.splitlines():
        bullet = re.match(r"^[-*\u2022]\s*(.+)$", raw_line)
        line_text = bullet.group(1) if bullet else raw_line
        line = html.escape(_polish_line(line_text), quote=False)
        line = _highlight_dates(line)
        if bullet:
            line = f"- {line}"
        lines.append(line)

    return sanitize_telegram_html("\n".join(lines))
