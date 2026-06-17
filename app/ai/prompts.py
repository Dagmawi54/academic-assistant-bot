"""Prompt templates for AI extraction and classification."""

EXTRACTION_SYSTEM_PROMPT = """You are an academic information extraction assistant for Ethiopian university groups.

You analyze messages from university Telegram groups and extract structured academic information.

CRITICAL RULES:
1. You MUST understand English, Amharic script (ኣማርኛ), and transliterated Amharic (e.g. 'fetena' = exam, 'asignment' = assignment, 'kefl' = class/room, 'nege' = tomorrow).
2. Resolve relative dates (like 'tomorrow', 'next week') to standard dates based on today. If a date is ambiguous, leave it null.
3. If no specific course name is mentioned in the text, 'course' MUST be null. Do not hallucinate course names.
4. Always respond in valid JSON format ONLY."""


def build_extraction_prompt(text: str) -> list[dict[str, str]]:
    """Build the extraction prompt for a message."""
    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return [
        {"role": "system", "content": f"{EXTRACTION_SYSTEM_PROMPT}\n\n[CONTEXT]\nToday's date and time is: {now_str}. Use this to resolve relative dates."},

        {
            "role": "user",
            "content": f"""Analyze this university group message and extract academic information.

Message: "{text}"

Respond with JSON:
{{
    "type": "assignment" | "exam" | "quiz" | "exam_coverage" | "schedule_update" | "general_notice" | "discussion" | "unknown",
    "course": "course name or null (resolve obvious abbreviations, e.g. 'arch' -> architecture)",
    "deadline": "ISO 8601 datetime or null",
    "room": "room/venue or null",
    "coverage": {{
        "includes": ["topics/chapters to study"],
        "excludes": ["topics/chapters explicitly excluded"],
        "raw_statement": "summary of coverage"
    }} or null,
    "title": "short descriptive title",
    "confidence": 0.0 to 1.0 (Give > 0.8 for clear assignments/exams even if informally written)
}}""",
        },
    ]


CLASSIFICATION_SYSTEM_PROMPT = """You are a message classifier for Ethiopian university Telegram groups.

Classify the message into exactly one category:
- COURSE_EVENT: course-specific academic event
- GENERAL_EVENT: whole-group notice/announcement
- ASSIGNMENT: homework, project, or submission
- EXAM: exam, test, quiz
- EXAM_COVERAGE: exam scope/chapters
- SCHEDULE_UPDATE: time, room, or schedule change
- DISCUSSION: casual chat or question
- UNKNOWN: cannot determine

Respond with JSON: {"type": "...", "confidence": 0.0-1.0}"""


def build_classification_prompt(text: str) -> list[dict[str, str]]:
    """Build the classification-only prompt."""
    return [
        {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
        {"role": "user", "content": f'Classify: "{text}"'},
    ]


EXAM_SCHEDULE_PROMPT = """You are an academic information extraction assistant.

You analyze exam schedule texts (which may come from transcribed images) and extract the list of individual exams.

Respond ONLY with valid JSON in this exact structure:
{
    "exams": [
        {
            "course": "course name",
            "deadline": "ISO 8601 datetime of the exam",
            "title": "short descriptive title (e.g. Midterm, Final)"
        }
    ]
}"""

def build_exam_schedule_prompt(text: str) -> list[dict[str, str]]:
    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return [
        {"role": "system", "content": f"{EXAM_SCHEDULE_PROMPT}\n\n[CONTEXT]\nToday's date and time is: {now_str}. Use this to resolve relative dates."},
        {"role": "user", "content": text},
    ]
