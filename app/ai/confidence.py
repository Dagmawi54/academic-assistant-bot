"""Confidence scoring and action thresholds."""

# Thresholds
AUTO_APPROVE_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.70


def should_auto_approve(confidence: float) -> bool:
    """Score >= 90% → auto-approve without admin review."""
    return confidence >= AUTO_APPROVE_THRESHOLD


def needs_review(confidence: float) -> bool:
    """Score 70-89% → optional admin review."""
    return REVIEW_THRESHOLD <= confidence < AUTO_APPROVE_THRESHOLD


def needs_manual_approval(confidence: float) -> bool:
    """Score < 70% → requires manual admin approval."""
    return confidence < REVIEW_THRESHOLD


def action_label(confidence: float) -> str:
    """Human-readable action label for a confidence score."""
    if should_auto_approve(confidence):
        return "Auto-approved"
    elif needs_review(confidence):
        return "Pending review"
    else:
        return "Requires manual approval"
