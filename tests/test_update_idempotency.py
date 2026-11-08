import pytest

from app.database import crud
from app.database.models import (
    AcademicItem,
    Course,
    DuplicateLog,
    Group,
    ProcessedMessage,
    Reminder,
    Topic,
)


@pytest.mark.asyncio
async def test_replayed_telegram_message_is_not_processed_twice(monkeypatch, session):
    from app.services import routing_service

    acknowledgements = []

    async def fake_send_message(**kwargs):
        acknowledgements.append(kwargs)

    async def no_ai_extraction(_text):
        return None

    def ignore_scheduler(_reminder_id, _send_time):
        return None

    monkeypatch.setattr(routing_service.bot, "send_message", fake_send_message)
    monkeypatch.setattr(routing_service, "_try_ai_extraction", no_ai_extraction)
    monkeypatch.setattr("app.reminders.scheduler.schedule_reminder", ignore_scheduler)

    group = await crud.create(session, Group(chat_id=-100700, semester=1, active=True))
    topic = await crud.create(
        session,
        Topic(
            group_id=group.id,
            chat_id=group.chat_id,
            message_thread_id=71,
            topic_name="Mathematics",
            topic_type="course",
            status="active",
        ),
    )
    course = await crud.create(
        session,
        Course(
            group_id=group.id,
            course_name="Mathematics",
            topic_id=topic.id,
            semester=1,
            active=True,
        ),
    )

    for _ in range(2):
        await routing_service.process_group_message(
            session,
            chat_id=group.chat_id,
            thread_id=topic.message_thread_id,
            text="assignment due Friday",
            user_id=123,
            message_id=9001,
        )

    items = await crud.get_all(session, AcademicItem, group_id=group.id)
    reminders = await crud.get_all(session, Reminder)
    receipts = await crud.get_all(session, ProcessedMessage, chat_id=group.chat_id)
    duplicate_logs = await crud.get_all(session, DuplicateLog, group_id=group.id)

    assert len(items) == 1
    assert items[0].course_id == course.id
    assert len(reminders) >= 1
    assert len(receipts) == 1
    assert duplicate_logs == []
    assert len(acknowledgements) == 1
