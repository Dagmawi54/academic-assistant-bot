"""Rule-based message classifier for academic content detection."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from dateutil import parser as dateparser

from app.utils.text import clean_text, contains_amharic


@dataclass
class ClassificationResult:
    """Result of message classification."""

    message_type: str  # ASSIGNMENT, EXAM, EXAM_COVERAGE, SCHEDULE_UPDATE, GENERAL_EVENT, COURSE_EVENT, DISCUSSION, UNKNOWN
    confidence: float  # 0.0 - 1.0
    course_hint: str | None = None
    deadline: datetime | None = None
    room: str | None = None
    coverage: str | None = None
    title: str | None = None


# ---------------------------------------------------------------------------
# Keyword patterns
# ---------------------------------------------------------------------------

ASSIGNMENT_KEYWORDS = {
    "assignment",
    "homework",
    "submit",
    "submission",
    "due",
    "deadline",
    "hand in",
    "turn in",
    "project",
    "lab report",
    "report",
}

EXAM_KEYWORDS = {
    "exam",
    "test",
    "midterm",
    "mid-term",
    "final",
    "final exam",
    "quiz",
    "assessment",
    "examination",
}

COVERAGE_KEYWORDS = {
    "coverage",
    "chapters",
    "covered",
    "covers",
    "scope",
    "topics covered",
    "exam coverage",
    "will cover",
}

SCHEDULE_KEYWORDS = {
    "schedule",
    "timetable",
    "class time",
    "room change",
    "room",
    "venue",
    "reschedule",
    "postpone",
    "cancelled",
    "moved to",
    "new time",
    "new room",
}

GENERAL_KEYWORDS = {
    "all students",
    "everyone",
    "notice",
    "announcement",
    "attention",
    "important",
    "urgent",
    "reminder",
    "registration",
    "grade",
    "result",
}

# Common course name patterns
COURSE_PATTERNS = re.compile(
    r"\b(database|networking|operating\s*systems?|data\s*structure|"
    r"algorithm|artificial\s*intelligence|machine\s*learning|"
    r"software\s*engineering|computer\s*network|web\s*development|"
    r"discrete\s*math|linear\s*algebra|calculus|physics|chemistry|"
    r"statistics|probability|compiler|digital\s*logic|"
    r"computer\s*architecture|graphics|security|"
    r"OOP|DBMS|OS|AI|ML|SE|CN|DS|DSA)\b",
    re.IGNORECASE,
)

# Date patterns
DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\b"),
    re.compile(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}(?:\s*,?\s*\d{4})?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*(?:\s+\d{4})?)\b",
        re.IGNORECASE,
    ),
]

# Room patterns
ROOM_PATTERN = re.compile(
    r"\b(?:room|hall|venue|class)\s*[:\-]?\s*([A-Z]?\d{1,4}[A-Z]?)\b",
    re.IGNORECASE,
)


def classify(text: str) -> ClassificationResult:
    """Classify a message using rule-based pattern matching.

    Returns a ClassificationResult with type, confidence, and extracted entities.
    """
    normalized = clean_text(text)
    lower = normalized.lower()

    # Extract entities
    course_hint = _extract_course(lower)
    deadline = _extract_date(normalized)
    room = _extract_room(normalized)

    # Score each category
    scores: dict[str, float] = {
        "ASSIGNMENT": _score_keywords(lower, ASSIGNMENT_KEYWORDS),
        "EXAM": _score_keywords(lower, EXAM_KEYWORDS),
        "EXAM_COVERAGE": _score_keywords(lower, COVERAGE_KEYWORDS),
        "SCHEDULE_UPDATE": _score_keywords(lower, SCHEDULE_KEYWORDS),
        "GENERAL_EVENT": _score_keywords(lower, GENERAL_KEYWORDS),
    }

    # Boost scores based on extracted entities
    if deadline:
        scores["ASSIGNMENT"] += 0.15
        scores["EXAM"] += 0.10

    if room:
        scores["SCHEDULE_UPDATE"] += 0.15
        scores["EXAM"] += 0.10

    if course_hint:
        # Presence of a course name boosts course-specific types
        for key in ("ASSIGNMENT", "EXAM", "EXAM_COVERAGE"):
            scores[key] += 0.10

    # Pick the best
    best_type = max(scores, key=scores.get)
    best_score = scores[best_type]

    if best_score < 0.15:
        # Too low confidence for any academic type
        return ClassificationResult(
            message_type="DISCUSSION",
            confidence=0.5,
            course_hint=course_hint,
        )

    # Determine if it's course-specific or general
    if best_type == "GENERAL_EVENT" and not course_hint:
        msg_type = "GENERAL_EVENT"
    elif best_type in ("ASSIGNMENT", "EXAM", "EXAM_COVERAGE") and course_hint:
        msg_type = best_type
    elif best_type == "SCHEDULE_UPDATE":
        msg_type = "SCHEDULE_UPDATE"
    else:
        msg_type = best_type

    # Cap confidence at 0.95 for rule-based (AI can go higher)
    confidence = min(best_score + 0.4, 0.95)

    # Determine coverage text
    coverage = None
    if msg_type == "EXAM_COVERAGE":
        coverage = _extract_coverage(normalized)

    return ClassificationResult(
        message_type=msg_type,
        confidence=confidence,
        course_hint=course_hint,
        deadline=deadline,
        room=room,
        coverage=coverage,
        title=_build_title(msg_type, course_hint),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _score_keywords(text: str, keywords: set[str]) -> float:
    """Score based on keyword presence. Returns 0.0 - 0.5."""
    matches = sum(1 for kw in keywords if kw in text)
    if matches == 0:
        return 0.0
    return min(matches * 0.15, 0.5)


def _extract_course(text: str) -> str | None:
    """Extract a course name hint from text."""
    match = COURSE_PATTERNS.search(text)
    return match.group(0).strip() if match else None


def _extract_date(text: str) -> datetime | None:
    """Extract a date from text."""
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return dateparser.parse(match.group(1), fuzzy=True)
            except (ValueError, TypeError):
                continue
    return None


def _extract_room(text: str) -> str | None:
    """Extract a room/venue identifier."""
    match = ROOM_PATTERN.search(text)
    return match.group(1) if match else None


def _extract_coverage(text: str) -> str | None:
    """Extract coverage/chapter information."""
    patterns = [
        re.compile(r"chapter[s]?\s*[:\-]?\s*(.+?)(?:\.|$)", re.IGNORECASE),
        re.compile(r"covers?\s+(.+?)(?:\.|$)", re.IGNORECASE),
        re.compile(r"ch\.?\s*(\d[\d\s,\-&and]+)", re.IGNORECASE),
    ]
    for p in patterns:
        match = p.search(text)
        if match:
            return match.group(1).strip()
    return None


def _build_title(msg_type: str, course: str | None) -> str | None:
    """Build a short title from type and course."""
    labels = {
        "ASSIGNMENT": "Assignment",
        "EXAM": "Exam",
        "EXAM_COVERAGE": "Exam Coverage",
        "SCHEDULE_UPDATE": "Schedule Update",
        "GENERAL_EVENT": "Notice",
    }
    label = labels.get(msg_type)
    if not label:
        return None
    if course:
        return f"{course.title()} — {label}"
    return label
