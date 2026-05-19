"""Tests for the confidence scoring system."""

import pytest

from app.ai.confidence import (
    should_auto_approve,
    needs_review,
    needs_manual_approval,
    action_label,
)


class TestConfidence:
    def test_auto_approve(self):
        assert should_auto_approve(0.95) is True
        assert should_auto_approve(0.90) is True
        assert should_auto_approve(0.89) is False

    def test_needs_review(self):
        assert needs_review(0.75) is True
        assert needs_review(0.70) is True
        assert needs_review(0.69) is False
        assert needs_review(0.91) is False

    def test_needs_manual(self):
        assert needs_manual_approval(0.50) is True
        assert needs_manual_approval(0.69) is True
        assert needs_manual_approval(0.70) is False

    def test_action_label(self):
        assert action_label(0.95) == "Auto-approved"
        assert action_label(0.80) == "Pending review"
        assert action_label(0.50) == "Requires manual approval"
