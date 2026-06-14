import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import crud
from app.database.models import AcademicItem, AuditLog, Group, Reminder


@pytest.mark.asyncio
async def test_acceptance_dashboard_only_marks_successful_runtime_acceptance(session):
    from app.services.acceptance_service import (
        ACCEPTANCE_FEATURES,
        record_acceptance_result,
        render_acceptance_dashboard,
    )

    await record_acceptance_result(
        session,
        feature_name="Exam Detection",
        result="passed",
        tester=101,
        reason="Telegram test succeeded",
    )
    await record_acceptance_result(
        session,
        feature_name="Reply Voice",
        result="failed",
        tester=101,
        reason="No transcription",
    )

    text = await render_acceptance_dashboard(session)

    assert "✅ Exam Detection" in text
    assert "❌ Reply Voice" in text
    assert "Telegram test succeeded" in text
    assert any("⚠️ Coverage Stitch" in text for _ in ACCEPTANCE_FEATURES)


@pytest.mark.asyncio
async def test_acceptance_records_are_stored_in_audit_log(session):
    from app.services.acceptance_service import record_acceptance_result

    entry = await record_acceptance_result(
        session,
        feature_name="Coverage Stitch",
        result="passed",
        tester=202,
        reason="Admin tapped Mark Passed after live flow",
        metadata={"item_id": 7},
    )

    assert entry.action == "acceptance_passed"
    payload = json.loads(entry.details)
    assert payload["feature_name"] == "Coverage Stitch"
    assert payload["result"] == "passed"
    assert payload["tester"] == 202
    assert payload["metadata"]["item_id"] == 7


@pytest.mark.asyncio
async def test_event_diagnostics_render_counts_from_database(session):
    from app.services.acceptance_service import collect_event_diagnostics, render_event_diagnostics

    group = await crud.create(session, Group(chat_id=-1001, department="CS", active=True))
    exam = await crud.create(
        session,
        AcademicItem(group_id=group.id, item_type="exam", title="Math Exam", status="active"),
    )
    await crud.create(
        session,
        AcademicItem(group_id=group.id, item_type="assignment", title="HW", status="active"),
    )
    await crud.create(
        session,
        AcademicItem(group_id=group.id, item_type="quiz", title="Quiz", status="active"),
    )
    await crud.create(
        session,
        AcademicItem(group_id=group.id, item_type="exam_coverage", title="Coverage", status="active"),
    )
    await crud.create(
        session,
        Reminder(item_id=exam.id, chat_id=group.chat_id, send_time=exam.created_at, sent=False, cancelled=False),
    )

    report = await collect_event_diagnostics(session)
    text = render_event_diagnostics(report)

    assert "Detected Exams" in text
    assert "<code>1</code>" in text
    assert "Coverage Records" in text
    assert "Quiz" in text


@pytest.mark.asyncio
async def test_callback_trace_middleware_persists_failure_and_stops_spinner(monkeypatch, session):
    from app.bot.middlewares.logging import CallbackTraceMiddleware
    from app.database import session as db_session_module

    monkeypatch.setattr(db_session_module, "async_session_factory", lambda: session)

    class DummyCallback:
        data = "menu:broken"
        from_user = SimpleNamespace(id=777)
        message = SimpleNamespace(chat=SimpleNamespace(id=555), edits=[])
        answers = []

        async def answer(self, text=None, **kwargs):
            self.answers.append({"text": text, **kwargs})

    async def edit_text(text, **kwargs):
        callback.message.edits.append({"text": text, **kwargs})

    callback = DummyCallback()
    callback.message.edit_text = edit_text

    async def broken_handler(event, data):
        raise RuntimeError("boom")

    middleware = CallbackTraceMiddleware()
    result = await middleware(broken_handler, callback, {})

    assert result is None
    assert callback.answers[-1]["show_alert"] is True
    assert "Reference" in callback.message.edits[-1]["text"]

    rows = (await session.execute(select(AuditLog).where(AuditLog.action == "callback_trace"))).scalars().all()
    phases = [json.loads(row.details)["phase"] for row in rows]
    assert "button_pressed" in phases
    assert "callback_failed" in phases


def test_communications_menu_uses_simplified_announcement_entrypoint():
    from app.admin.menus import communications_menu, main_menu

    main_text = [button.text for row in main_menu().inline_keyboard for button in row]
    comm_text = [button.text for row in communications_menu().inline_keyboard for button in row]

    assert any("Broadcasts" in text or "Announcements" in text for text in main_text)
    assert any("Announcement" in text for text in comm_text)
    assert "Raw Broadcast" not in main_text
    assert "Raw Broadcast" not in comm_text
    assert "Direct Broadcast" not in comm_text


@pytest.mark.asyncio
async def test_ask_diagnostics_renders_media_support_and_provider_sections(session):
    from app.services.acceptance_service import collect_ask_diagnostics, render_ask_diagnostics

    report = await collect_ask_diagnostics(session)
    text = render_ask_diagnostics(report)

    assert "/ask Diagnostics" in text
    assert "Chat Provider" in text
    assert "Academic Extraction" in text
    assert "PDF: ✅" in text
    assert "Voice: ✅" in text
    assert "Conversation Memory" in text
