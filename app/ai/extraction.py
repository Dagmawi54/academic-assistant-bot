"""Parse structured extraction results from AI responses."""

from datetime import datetime
from typing import Any

from dateutil import parser as dateparser

from app.logging import get_logger

logger = get_logger("extraction")

# Map AI types to internal classification types
TYPE_MAP = {
    "assignment": "ASSIGNMENT",
    "exam": "EXAM",
    "quiz": "EXAM",
    "exam_coverage": "EXAM_COVERAGE",
    "schedule_update": "SCHEDULE_UPDATE",
    "general_notice": "GENERAL_EVENT",
    "general_event": "GENERAL_EVENT",
    "course_event": "COURSE_EVENT",
    "discussion": "DISCUSSION",
    "unknown": "UNKNOWN",
}


def parse_extraction(data: dict[str, Any]) -> dict[str, Any]:
    """Parse and normalize AI extraction output.

    Returns a clean dict with normalized fields.
    """
    if not data or "raw" in data:
        return {}

    try:
        result: dict[str, Any] = {
            "type": TYPE_MAP.get(data.get("type", "unknown"), "UNKNOWN"),
            "course": data.get("course"),
            "deadline": _parse_datetime(data.get("deadline")),
            "room": data.get("room"),
            "coverage": data.get("coverage"),
            "title": data.get("title"),
            "confidence": float(data.get("confidence", 0.0)),
        }
        return result
    except Exception:
        logger.exception("extraction_parse_failed", data=str(data)[:200])
        return {}


def _parse_datetime(value: Any) -> datetime | None:
    """Safely parse a datetime string."""
    if not value or value == "null":
        return None
    try:
        return dateparser.parse(str(value))
    except (ValueError, TypeError):
        return None
