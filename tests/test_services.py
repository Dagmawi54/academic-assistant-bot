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

