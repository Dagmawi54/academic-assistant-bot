"""Tests for the rule-based message classifier."""

import pytest
from datetime import datetime

from app.routing.classifier import classify


class TestClassification:
    """Test message classification types."""

    def test_assignment_detected(self):
        result = classify("Database assignment due May 25, submit on LMS")
        assert result.message_type == "ASSIGNMENT"
        assert result.confidence > 0.5

    def test_exam_detected(self):
        result = classify("Networking final exam on June 10 in room 302")
        assert result.message_type == "EXAM"
        assert result.confidence > 0.5

    def test_exam_coverage(self):
        result = classify("Mid exam coverage: chapters 1-5 will be covered")
        assert result.message_type == "EXAM_COVERAGE"
        assert result.confidence > 0.5

    def test_schedule_update(self):
        result = classify("Class moved to room 205, new time 2:00 PM")
        assert result.message_type == "SCHEDULE_UPDATE"
        assert result.confidence > 0.5

    def test_general_event(self):
        result = classify("Attention all students: registration deadline tomorrow")
        assert result.message_type in ("GENERAL_EVENT", "ASSIGNMENT")
        assert result.confidence > 0.5

    def test_discussion_message(self):
        result = classify("Hey does anyone have the notes from today?")
        assert result.message_type == "DISCUSSION"

    def test_short_casual(self):
        result = classify("ok thanks")
        assert result.message_type == "DISCUSSION"


class TestEntityExtraction:
    """Test entity extraction from messages."""

    def test_course_extraction(self):
        result = classify("Database assignment due May 22")
        assert result.course_hint is not None
        assert "database" in result.course_hint.lower()

    def test_date_extraction_us_format(self):
        result = classify("Submit homework by 05/25/2026")
        assert result.deadline is not None
        assert result.deadline.month == 5
        assert result.deadline.day == 25

    def test_date_extraction_text_format(self):
        result = classify("Assignment due May 22, 2026")
        assert result.deadline is not None
        assert result.deadline.month == 5

    def test_room_extraction(self):
        result = classify("Exam in room 302")
        assert result.room == "302"

    def test_coverage_extraction(self):
        result = classify("Exam covers chapters 1-5")
        assert result.coverage is not None
        assert "1-5" in result.coverage

    def test_coverage_chapter_range(self):
        result = classify("covers chapter 1-4")
        assert result.message_type == "EXAM_COVERAGE"
        assert result.coverage is not None
        assert "1-4" in result.coverage

    def test_coverage_exclusion(self):
        result = classify("excluding recursion")
        assert result.message_type == "EXAM_COVERAGE"
        assert result.coverage is not None
        assert "recursion" in result.coverage.lower()


class TestMixedLanguage:
    """Test handling of Amharic-English mixed text."""

    def test_amharic_keywords(self):
        # "fetena" = exam, "nw" = is
        result = classify("DB fetena be May 22 nw")
        # After normalization: "DB exam by May 22 is"
        assert result.message_type in ("EXAM", "ASSIGNMENT")

    def test_assignment_amharic(self):
        # "sira" = assignment
        result = classify("database sira deadline May 25")
        assert result.message_type == "ASSIGNMENT"
