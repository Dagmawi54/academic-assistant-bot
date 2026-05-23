from types import SimpleNamespace

import pytest

from app.database import crud
from app.database.models import AcademicItem, Course, DuplicateLog, Group, Topic, User


class DummyState:
    def __init__(self):
        self.data = {}
        self.current_state = None

    async def clear(self):
        self.data = {}
        self.current_state = None

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)

    async def set_state(self, state):
        self.current_state = state


class DummyMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append({"text": text, **kwargs})


class DummyCallback:
    def __init__(self, data: str, user_id: int = 1):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = DummyMessage()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append({"text": text, **kwargs})


@pytest.mark.asyncio
async def test_targeted_push_course_selection_uses_topic_id(session):
    from app.admin.states import AnnouncementStates
    from app.bot.handlers.communications import cb_select_course

    group = await crud.create(session, Group(chat_id=-100501, department="Math", active=True))
    topic = await crud.create(
        session,
        Topic(
            group_id=group.id,
            chat_id=group.chat_id,
            message_thread_id=55,
            topic_name="Math Topic",
            topic_type="course",
            status="active",
        ),
    )
    course = await crud.create(
        session,
        Course(
            group_id=group.id,
            course_name="Math",
            topic_id=topic.id,
            semester=1,
            active=True,
        ),
    )

    state = DummyState()
    callback = DummyCallback(f"course:{course.id}")

    await cb_select_course(callback, state, session)

    assert state.data["topic_id"] == topic.id
    assert state.current_state == AnnouncementStates.waiting_content
    assert callback.message.edits
    assert "Targeting" in callback.message.edits[-1]["text"]


@pytest.mark.asyncio
async def test_manual_exam_coverage_creation_persists_item_and_posts(monkeypatch, session):
    from app.services.exam_coverage_service import create_exam_coverage_entry

    group = await crud.create(session, Group(chat_id=-100777, department="Math", active=True))
    topic = await crud.create(
        session,
        Topic(
            group_id=group.id,
            chat_id=group.chat_id,
            message_thread_id=88,
            topic_name="Math Topic",
            topic_type="course",
            status="active",
        ),
    )
    course = await crud.create(
        session,
        Course(
            group_id=group.id,
            course_name="Math",
            topic_id=topic.id,
            semester=1,
            active=True,
        ),
    )

    sent = []

    async def fake_send_message(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr("app.services.exam_coverage_service.bot.send_message", fake_send_message)

    item = await create_exam_coverage_entry(
        session,
        group=group,
        course=course,
        topic=topic,
        exam_type="mid",
        coverage_text="chapters 1-4",
        notes="excluding recursion",
        created_by=42,
    )

    saved = await crud.get_by_id(session, AcademicItem, item.id)
    assert saved is not None
    assert saved.item_type == "exam_coverage"
    assert "chapters 1-4" in (saved.coverage or "")
    assert "excluding recursion" in (saved.coverage or "")
    assert sent
    assert sent[0]["message_thread_id"] == 88
    assert "chapters 1-4" in sent[0]["text"]


@pytest.mark.asyncio
async def test_duplicate_details_render_single_record(session):
    from app.services.event_service import get_duplicate_detail

    group = await crud.create(session, Group(chat_id=-100909, department="Math", active=True))
    item = await crud.create(
        session,
        AcademicItem(
            group_id=group.id,
            item_type="exam",
            title="Math Exam",
            raw_text="exam on monday",
            status="active",
            confidence=0.9,
        ),
    )
    duplicate = await crud.create(
        session,
        DuplicateLog(
            group_id=group.id,
            existing_item_id=item.id,
            source_message_id=321,
            reason="Semantic duplicate",
            raw_text="exam monday",
        ),
    )

    detail = await get_duplicate_detail(session, group.id, duplicate.id)

    assert detail is not None
    assert detail.id == duplicate.id
    assert detail.existing_item_id == item.id
    assert detail.raw_text == "exam monday"


def test_announcement_formatter_highlights_urgency_and_dates():
    from app.services.announcement_formatter import format_announcement_html

    formatted = format_announcement_html("assignment due Friday at 5pm")

    assert "<b>" in formatted
    assert "Friday" in formatted
    assert "assignment" in formatted.lower()
