"""Tests for runtime diagnostics rendering."""

from app.services.runtime_diagnostics import render_runtime_diagnostics


def test_render_runtime_diagnostics_includes_core_sections():
    report = {
        "bot": {
            "username": "academicgroupmanagementbot",
            "can_read_all_group_messages": False,
            "can_join_groups": True,
        },
        "telegram": {
            "mode": "polling",
            "webhook_url_set": False,
            "pending_update_count": 0,
        },
        "redis": {"cache_connected": True, "fsm_connected": True, "storage_type": "RedisStorage"},
        "database": {
            "ok": True,
            "groups": 1,
            "topics": 3,
            "courses": 2,
            "academic_items": 4,
            "reminders": 3,
        },
        "scheduler": {"running": True, "job_count": 2, "jobs": ["reminder_1 -> 2026-05-24T09:00:00"]},
        "groups": [{"id": 1, "chat_id": -1001, "label": "CS", "topics": ["Math -> Math"]}],
    }

    text = render_runtime_diagnostics(report)

    assert "academicgroupmanagementbot" in text
    assert "can_read_all_group_messages=False" in text
    assert "RedisStorage" in text
    assert "groups=1 topics=3 courses=2" in text
    assert "reminder_1" in text
