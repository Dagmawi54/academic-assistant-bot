import pytest
from app.database import crud
from app.database.models import Group, Topic, Course, User, AcademicItem, Reminder
from app.services.semester_service import close_semester, activate_semester


@pytest.mark.asyncio
async def test_group_registration(session):
    """Test standard group and owner registration."""
    # 1. Create a group
    group = Group(chat_id=-100123456, active=True)
    group = await crud.create(session, group)
    assert group.id is not None
    assert group.chat_id == -100123456

    # 2. Register owner
    user = User(telegram_user_id=999, group_id=group.id, role="owner")
    user = await crud.create(session, user)
    assert user.id is not None
    
    # 3. Verify retrieval
    retrieved = await crud.get_group_by_chat_id(session, -100123456)
    assert retrieved is not None
    assert retrieved.id == group.id

@pytest.mark.asyncio
async def test_topic_and_course_linking(session):
    """Test linking a topic to a course."""
    group = await crud.create(session, Group(chat_id=-100999, active=True))
    
    # Create topic
    topic = await crud.create(session, Topic(
        group_id=group.id,
        chat_id=group.chat_id,
        message_thread_id=42,
        topic_name="Mathematics",
        topic_type="course",
        status="active"
    ))
    
    # Create course securely linked to topic
    course = await crud.create(session, Course(
        group_id=group.id,
        course_name="MATH 101",
        topic_id=topic.id,
        semester=1,
        active=True
    ))
    
    assert course.topic_id == topic.id
    
    # Test lookup logic
    fetched_course = await crud.get_course_by_name(session, group.id, "MATH 101")
    assert fetched_course is not None
    assert fetched_course.topic_id == topic.id

@pytest.mark.asyncio
async def test_semester_lifecycle(session):
    """Test that closing a semester properly archives topics and courses."""
    group = await crud.create(session, Group(chat_id=-100444, semester=1, active=True))
    
    topic = await crud.create(session, Topic(
        group_id=group.id, chat_id=group.chat_id, message_thread_id=1, topic_name="Bio", topic_type="course", status="active"
    ))
    course = await crud.create(session, Course(
        group_id=group.id, course_name="BIO 101", topic_id=topic.id, semester=1, active=True
    ))
    
    # Close semester
    await close_semester(session, group.id)
    
    # Verify state isolated
    closed_topic = await crud.get_topic(session, group.chat_id, 1)
    assert closed_topic.status == "closed"
    
    closed_course = await crud.get_by_id(session, Course, course.id)
    assert closed_course.active is False
    
    # Activate new semester
    await activate_semester(session, group.id, 2)
    updated_group = await crud.get_by_id(session, Group, group.id)
    assert updated_group.semester == 2

@pytest.mark.asyncio
async def test_reminder_generation_logic(session):
    """Test that creating an AcademicItem triggers reminder records in DB."""
    from app.services.reminder_service import create_reminders_for_item
    from datetime import timedelta
    from app.utils.timezone import now_addis
    from tests.conftest import TestSessionFactory

    deadline = now_addis() + timedelta(days=5)

    # Create group + item in a committed transaction so the service can see them
    async with TestSessionFactory() as setup_session:
        async with setup_session.begin():
            group = Group(chat_id=-1, active=True)
            setup_session.add(group)
            await setup_session.flush()

            item = AcademicItem(
                group_id=group.id,
                item_type="assignment",
                title="Test Task",
                deadline=deadline,
                source_chat_id=-1,
                status="active"
            )
            setup_session.add(item)
            await setup_session.flush()
            item_id = item.id

    # Call the service (opens its own session internally)
    await create_reminders_for_item(item_id)

    # Verify reminders were created
    async with TestSessionFactory() as check_session:
        reminders = await crud.get_all(check_session, Reminder, item_id=item_id)
        assert len(reminders) >= 1, f"Expected reminders, got {len(reminders)}"
        for r in reminders:
            assert r.sent is False
            assert r.cancelled is False


@pytest.mark.asyncio
async def test_classifier_discussion_filter():
    """Test that casual chat is classified as DISCUSSION."""
    from app.routing.classifier import classify

    result = classify("hey guys what's up, anyone going to lunch?")
    assert result.message_type == "DISCUSSION"
    assert result.confidence >= 0.3

    result2 = classify("Database assignment due tomorrow, submit on LMS!")
    assert result2.message_type == "ASSIGNMENT"
    assert result2.confidence >= 0.5


@pytest.mark.asyncio
async def test_topic_context_exam_creates_item_and_ack(monkeypatch):
    """A short exam message inside a course topic should infer the course."""
    from app.services.routing_service import process_group_message
    from tests.conftest import TestSessionFactory

    sent_messages = []

    async def fake_send_message(**kwargs):
        sent_messages.append(kwargs)

    monkeypatch.setattr("app.services.routing_service.bot.send_message", fake_send_message)

    async def fake_ai_extraction(text):
        return None

    monkeypatch.setattr("app.services.routing_service._try_ai_extraction", fake_ai_extraction)

    async with TestSessionFactory() as db:
        async with db.begin():
            group = await crud.create(db, Group(chat_id=-100222, semester=1, active=True))
            topic = await crud.create(
                db,
                Topic(
                    group_id=group.id,
                    chat_id=group.chat_id,
                    message_thread_id=42,
                    topic_name="Math",
                    topic_type="course",
                    status="active",
                ),
            )
            course = await crud.create(
                db,
                Course(
                    group_id=group.id,
                    course_name="Math",
                    semester=1,
                    topic_id=topic.id,
                    active=True,
                ),
            )

            await process_group_message(
                session=db,
                chat_id=group.chat_id,
                thread_id=topic.message_thread_id,
                text="exam on monday",
                user_id=123,
                message_id=777,
            )

            items = await crud.get_all(db, AcademicItem, group_id=group.id)
            reminders = await crud.get_all(db, Reminder)

    assert len(items) == 1
    assert items[0].item_type == "exam"
    assert items[0].course_id == course.id
    assert items[0].deadline is not None
    assert items[0].source_message_link is not None
    assert reminders
    assert sent_messages
    assert sent_messages[0]["message_thread_id"] == 42
    assert "Exam added" in sent_messages[0]["text"]
    assert "Confidence" not in sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_topic_context_assignment_creates_item_reminders_and_ack(monkeypatch):
    """A short assignment message inside a course topic should infer the course."""
    from app.services.routing_service import process_group_message
    from tests.conftest import TestSessionFactory

    sent_messages = []

    async def fake_send_message(**kwargs):
        sent_messages.append(kwargs)

    monkeypatch.setattr("app.services.routing_service.bot.send_message", fake_send_message)

    async def fake_ai_extraction(text):
        return None

    monkeypatch.setattr("app.services.routing_service._try_ai_extraction", fake_ai_extraction)

    async with TestSessionFactory() as db:
        async with db.begin():
            group = await crud.create(db, Group(chat_id=-100333, semester=1, active=True))
            topic = await crud.create(
                db,
                Topic(
                    group_id=group.id,
                    chat_id=group.chat_id,
                    message_thread_id=84,
                    topic_name="Data Structures",
                    topic_type="course",
                    status="active",
                ),
            )
            course = await crud.create(
                db,
                Course(
                    group_id=group.id,
                    course_name="Data Structures",
                    semester=1,
                    topic_id=topic.id,
                    active=True,
                ),
            )

            await process_group_message(
                session=db,
                chat_id=group.chat_id,
                thread_id=topic.message_thread_id,
                text="assignment due friday",
                user_id=123,
                message_id=778,
            )

            items = await crud.get_all(db, AcademicItem, group_id=group.id)
            reminders = await crud.get_all(db, Reminder)

    assert len(items) == 1
    assert items[0].item_type == "assignment"
    assert items[0].course_id == course.id
    assert items[0].deadline is not None
    assert reminders
    assert all(r.thread_id == 84 for r in reminders)
    assert sent_messages
    assert sent_messages[0]["message_thread_id"] == 84
    assert "Assignment deadline recorded" in sent_messages[0]["text"]
    assert f"{len(reminders)} reminder" in sent_messages[0]["text"]
    assert "Confidence" not in sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_relative_deadline_assignment_creates_reminders(monkeypatch):
    """Relative quantity deadlines should become real deadlines and reminders."""
    from app.services.routing_service import process_group_message
    from tests.conftest import TestSessionFactory

    sent_messages = []

    async def fake_send_message(**kwargs):
        sent_messages.append(kwargs)

    monkeypatch.setattr("app.services.routing_service.bot.send_message", fake_send_message)

    async def fake_ai_extraction(text):
        return None

    monkeypatch.setattr("app.services.routing_service._try_ai_extraction", fake_ai_extraction)

    async with TestSessionFactory() as db:
        async with db.begin():
            group = await crud.create(db, Group(chat_id=-100334, semester=1, active=True))
            topic = await crud.create(
                db,
                Topic(
                    group_id=group.id,
                    chat_id=group.chat_id,
                    message_thread_id=85,
                    topic_name="Software Engineering",
                    topic_type="course",
                    status="active",
                ),
            )
            course = await crud.create(
                db,
                Course(
                    group_id=group.id,
                    course_name="Software Engineering",
                    semester=1,
                    topic_id=topic.id,
                    active=True,
                ),
            )

            await process_group_message(
                session=db,
                chat_id=group.chat_id,
                thread_id=topic.message_thread_id,
                text="assignment due 3 weeks from now",
                user_id=123,
                message_id=780,
            )

            items = await crud.get_all(db, AcademicItem, group_id=group.id)
            reminders = await crud.get_all(db, Reminder)

    assert len(items) == 1
    assert items[0].item_type == "assignment"
    assert items[0].course_id == course.id
    assert items[0].deadline is not None
    assert reminders
    assert all(r.thread_id == 85 for r in reminders)
    assert sent_messages
    assert "Assignment deadline recorded" in sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_exact_exam_on_tuesday_pipeline_logs_item_reminders_and_ack(monkeypatch):
    """The live-critical short exam phrase should traverse the full Academic OS path."""
    from app.services.routing_service import process_group_message
    from tests.conftest import TestSessionFactory

    sent_messages = []

    async def fake_send_message(**kwargs):
        sent_messages.append(kwargs)

    monkeypatch.setattr("app.services.routing_service.bot.send_message", fake_send_message)

    async def fake_ai_extraction(text):
        return None

    monkeypatch.setattr("app.services.routing_service._try_ai_extraction", fake_ai_extraction)

    async with TestSessionFactory() as db:
        async with db.begin():
            group = await crud.create(db, Group(chat_id=-100444, semester=1, active=True))
            topic = await crud.create(
                db,
                Topic(
                    group_id=group.id,
                    chat_id=group.chat_id,
                    message_thread_id=99,
                    topic_name="Mathematics",
                    topic_type="course",
                    status="active",
                ),
            )
            course = await crud.create(
                db,
                Course(
                    group_id=group.id,
                    course_name="Mathematics",
                    semester=1,
                    topic_id=topic.id,
                    active=True,
                ),
            )

            await process_group_message(
                session=db,
                chat_id=group.chat_id,
                thread_id=topic.message_thread_id,
                text="exam on Tuesday",
                user_id=123,
                message_id=779,
            )

            items = await crud.get_all(db, AcademicItem, group_id=group.id)
            reminders = await crud.get_all(db, Reminder)

    assert len(items) == 1
    assert items[0].item_type == "exam"
    assert items[0].course_id == course.id
    assert items[0].deadline is not None
    assert reminders
    assert all(reminder.thread_id == 99 for reminder in reminders)
    assert sent_messages
    assert sent_messages[0]["message_thread_id"] == 99
    assert "Exam added" in sent_messages[0]["text"]
    assert "Confidence" not in sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_academic_lookup_question_does_not_create_item(monkeypatch):
    """Questions like 'when is the exam' should not be recorded as new events."""
    from app.services.routing_service import process_group_message
    from tests.conftest import TestSessionFactory

    sent_messages = []

    async def fake_send_message(**kwargs):
        sent_messages.append(kwargs)

    monkeypatch.setattr("app.services.routing_service.bot.send_message", fake_send_message)

    async def fake_ai_extraction(text):
        raise AssertionError("Lookup questions should be dropped before AI extraction")

    monkeypatch.setattr("app.services.routing_service._try_ai_extraction", fake_ai_extraction)

    async with TestSessionFactory() as db:
        async with db.begin():
            group = await crud.create(db, Group(chat_id=-100445, semester=1, active=True))
            topic = await crud.create(
                db,
                Topic(
                    group_id=group.id,
                    chat_id=group.chat_id,
                    message_thread_id=100,
                    topic_name="Mathematics",
                    topic_type="course",
                    status="active",
                ),
            )
            await crud.create(
                db,
                Course(
                    group_id=group.id,
                    course_name="Mathematics",
                    semester=1,
                    topic_id=topic.id,
                    active=True,
                ),
            )

            await process_group_message(
                session=db,
                chat_id=group.chat_id,
                thread_id=topic.message_thread_id,
                text="when is the exam",
                user_id=123,
                message_id=781,
            )

            items = await crud.get_all(db, AcademicItem, group_id=group.id)

    assert items == []
    assert sent_messages == []

