"""Structured parsing and stitching for exam coverage text."""

from __future__ import annotations

import json
import re
from typing import Any


def parse_coverage_text(text: str, exam_type: str | None = None) -> dict[str, Any]:
    """Parse rough coverage notes into a stable JSON-friendly structure."""
    raw = (text or "").strip()
    normalized = re.sub(r"\s+", " ", raw.lower())

    included: list[str] = []
    excluded: list[str] = []

    range_matches = re.findall(r"\b(?:chapter|chapters|unit|units)\s+(\d+)\s*(?:-|to|through)\s*(\d+)", normalized)
    for start, end in range_matches:
        word = "chapters" if "chapter" in normalized else "units"
        included.append(f"{word} {start}-{end}")

    only_match = re.search(r"\b(?:unit|units|chapter|chapters)\s+([0-9,\sand]+)\s+only\b", normalized)
    if only_match:
        scope = only_match.group(1).replace(" and ", ", ")
        word = "chapters" if "chapter" in normalized else "units"
        included.append(f"{word} {scope} only")

    topic_patterns = [
        r"\bcovers?\s+([^.;|]+)",
        r"\bmid\s+covers?\s+([^.;|]+)",
        r"\bfinal\s+covers?\s+([^.;|]+)",
    ]
    for pattern in topic_patterns:
        match = re.search(pattern, normalized)
        if match:
            value = _clean_topic(match.group(1))
            if value:
                included.append(value)

    for pattern in (r"\bexcluding\s+([^.;|]+)", r"\bexcept\s+([^.;|]+)"):
        match = re.search(pattern, normalized)
        if match:
            value = _clean_topic(match.group(1))
            if value:
                excluded.append(value)

    detected_exam_type = exam_type or _detect_exam_type(normalized)
    summary = _build_summary(included, excluded, detected_exam_type, raw)

    return {
        "included_topics": _dedupe(included),
        "excluded_topics": _dedupe(excluded),
        "exam_type": detected_exam_type,
        "coverage_summary": summary,
        "raw_statement": raw,
    }


def merge_coverage(existing: str | None, incoming: str | None = None) -> dict[str, Any]:
    """Merge old and new coverage strings/dicts without duplicating topics."""
    existing_data = _load_coverage(existing)
    incoming_data = _load_coverage(incoming)

    combined_raw = " | ".join(
        part
        for part in [
            existing_data.get("raw_statement"),
            incoming_data.get("raw_statement"),
        ]
        if part
    )

    return {
        "included_topics": _dedupe(
            list(existing_data.get("included_topics") or [])
            + list(incoming_data.get("included_topics") or [])
        ),
        "excluded_topics": _dedupe(
            list(existing_data.get("excluded_topics") or [])
            + list(incoming_data.get("excluded_topics") or [])
        ),
        "exam_type": incoming_data.get("exam_type") or existing_data.get("exam_type") or "exam",
        "coverage_summary": _build_summary(
            list(existing_data.get("included_topics") or []) + list(incoming_data.get("included_topics") or []),
            list(existing_data.get("excluded_topics") or []) + list(incoming_data.get("excluded_topics") or []),
            incoming_data.get("exam_type") or existing_data.get("exam_type") or "exam",
            combined_raw,
        ),
        "raw_statement": combined_raw or existing or incoming or "",
    }


def dump_coverage(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def render_coverage_summary(coverage: str | None) -> str:
    data = _load_coverage(coverage)
    return data.get("coverage_summary") or data.get("raw_statement") or coverage or "Unknown"


def _load_coverage(value: str | None) -> dict[str, Any]:
    if not value:
        return parse_coverage_text("")
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return {
                "included_topics": list(parsed.get("included_topics") or []),
                "excluded_topics": list(parsed.get("excluded_topics") or []),
                "exam_type": parsed.get("exam_type") or "exam",
                "coverage_summary": parsed.get("coverage_summary") or "",
                "raw_statement": parsed.get("raw_statement") or "",
            }
    except (TypeError, ValueError):
        pass
    return parse_coverage_text(value)


def _detect_exam_type(text: str) -> str:
    if "practical" in text:
        return "practical exam"
    if "mcq" in text:
        return "mcq"
    if "lab" in text and "theory" in text:
        return "lab + theory"
    if "quiz" in text:
        return "quiz"
    if "mid" in text:
        return "mid exam"
    if "final" in text:
        return "final exam"
    return "exam"


def _clean_topic(value: str) -> str:
    value = re.sub(r"\b(will be covered|is covered|are covered)\b", "", value)
    value = value.strip(" .,:;|")
    return value


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = re.sub(r"\s+", " ", str(value)).strip()
        key = clean.lower()
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
    return result


def _build_summary(included: list[str], excluded: list[str], exam_type: str | None, raw: str) -> str:
    parts = []
    if exam_type:
        parts.append(str(exam_type).replace("_", " ").title())
    clean_included = _dedupe(included)
    clean_excluded = _dedupe(excluded)
    if clean_included:
        parts.append("covers " + ", ".join(clean_included))
    if clean_excluded:
        parts.append("excluding " + ", ".join(clean_excluded))
    if not parts and raw:
        return raw
    return "; ".join(parts) if parts else "Coverage not specified"
