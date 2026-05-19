"""Tests for the notification formatter."""

import pytest
from datetime import datetime

from app.reminders.formatter import format_reminder, format_academic_notification
from app.database.models import AcademicItem
from app.routing.classifier import ClassificationResult


class TestReminderFormatter:
    """Test reminder notification formatting."""

    def test_basic_reminder(self):
        item = AcademicItem(
            id=1,
            group_id=1,
            item_type="assignment",
            title="Database Assignment 1",
            deadline=datetime(2026, 5, 22, 23, 59),
        )
        text = format_reminder(item)
        assert "Database Assignment 1" in text
        assert "Reminder" in text
        assert "Deadline" in text

    def test_reminder_with_room(self):
        item = AcademicItem(
            id=1,
            group_id=1,
            item_type="exam",
            title="Networking Final",
            deadline=datetime(2026, 6, 10, 9, 0),
            room="302",
        )
        text = format_reminder(item)
        assert "302" in text
        assert "Room" in text

    def test_reminder_with_coverage(self):
        item = AcademicItem(
            id=1,
            group_id=1,
            item_type="exam",
            title="AI Mid Exam",
            deadline=datetime(2026, 5, 15, 10, 0),
            coverage="Chapters 1-5",
        )
        text = format_reminder(item)
        assert "Chapters 1\\-5" in text or "Chapters 1-5" in text


class TestAcademicNotification:
    """Test academic item notification formatting."""

    def test_assignment_notification(self):
        result = ClassificationResult(
            message_type="ASSIGNMENT",
            confidence=0.95,
            course_hint="Database",
            deadline=datetime(2026, 5, 22, 23, 59),
            title="Database — Assignment",
        )
        text = format_academic_notification(result)
        assert "Assignment" in text
        assert "Database" in text

    def test_exam_notification(self):
        result = ClassificationResult(
            message_type="EXAM",
            confidence=0.90,
            course_hint="Networking",
            deadline=datetime(2026, 6, 10, 9, 0),
            room="302",
            title="Networking — Exam",
        )
        text = format_academic_notification(result)
        assert "Exam" in text
        assert "302" in text
