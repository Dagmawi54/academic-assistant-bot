"""Notification message formatters for reminders and academic items."""

from app.database.models import AcademicItem
from app.routing.classifier import ClassificationResult
from app.utils.text import escape_md
from app.utils.timezone import format_datetime


def format_reminder(item: AcademicItem) -> str:
    """Format a reminder notification message.

    Style: calm, readable, non-spammy, minimal emojis.
    Uses Telegram MarkdownV2.
    """
    # Title
    title = escape_md(item.title or f"{item.item_type.title()}")
    lines = [f"<b>{title} — Reminder</b>"]
    lines.append("")

    # Deadline
    if item.deadline:
        lines.append(f"<code>Deadline</code>")
        lines.append(f"{escape_md(format_datetime(item.deadline))} \\(Addis Ababa Time\\)")
        lines.append("")

    # Room
    if item.room:
        lines.append(f"<code>Room</code>")
        lines.append(escape_md(item.room))
        lines.append("")

    # Coverage
    if item.coverage:
        lines.append(f"<code>Coverage</code>")
        lines.append(escape_md(item.coverage))
        lines.append("")

    return "\n".join(lines)


def format_academic_notification(
    classification: ClassificationResult,
    item: AcademicItem | None = None,
) -> str:
    """Format a new academic item notification (posted on detection).

    Style: calm, structured, informative.
    Uses Telegram HTML format.
    """
    import html
    type_labels = {
        "ASSIGNMENT": "📝 Assignment",
        "EXAM": "📋 Exam",
        "EXAM_COVERAGE": "📖 Exam Coverage",
        "SCHEDULE_UPDATE": "🔄 Schedule Update",
        "GENERAL_EVENT": "📢 Notice",
        "COURSE_EVENT": "📌 Course Update",
    }

    label = type_labels.get(classification.message_type, "📌 Update")
    title = html.escape(classification.title or classification.message_type.replace("_", " ").title())

    lines = [f"<b>{html.escape(label)}</b>"]
    lines.append(f"<i>{title}</i>")
    lines.append("")

    if classification.deadline:
        lines.append(f"<code>Deadline</code>")
        lines.append(f"{html.escape(format_datetime(classification.deadline))}")
        lines.append("")

    if classification.room:
        lines.append(f"<code>Room</code> {html.escape(classification.room)}")
        lines.append("")

    if classification.coverage:
        lines.append(f"<code>Coverage</code>")
        lines.append(html.escape(classification.coverage))
        lines.append("")

    if classification.course_hint:
        lines.append(f"<code>Course</code> {html.escape(classification.course_hint)}")

    return "\n".join(lines)
