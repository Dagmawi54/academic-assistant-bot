import asyncio
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import personal_task_service as service

MIGRATION = ROOT / "migrations" / "0016_personal_tasks.sql"


class _Statement:
    def __init__(self, database, query):
        self.database = database
        self.query = query
        self.values = ()

    def bind(self, *values):
        self.values = values
        return self

    async def first(self):
        cursor = self.database.connection.execute(self.query, self.values)
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    async def all(self):
        cursor = self.database.connection.execute(self.query, self.values)
        return {"results": [dict(row) for row in cursor.fetchall()]}

    async def run(self):
        cursor = self.database.connection.execute(self.query, self.values)
        self.database.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class _D1:
    def __init__(self, connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def prepare(self, query):
        return _Statement(self, query)

    async def batch(self, statements):
        with self.connection:
            return [self.connection.execute(statement.query, statement.values) for statement in statements]


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(MIGRATION.read_text(encoding="utf-8"))
    yield _D1(connection)
    connection.close()


def test_personal_task_schema_has_owner_timezone_and_due_indexes(database):
    tables = {
        row[0]
        for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    indexes = {
        row[0]
        for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }

    assert {"assistant_user_settings", "personal_tasks", "personal_task_reminders", "assistant_pending_actions"} <= tables
    assert "ix_personal_tasks_user_status_due" in indexes
    assert "ix_personal_task_reminders_due" in indexes


def test_personal_task_and_reminder_constraints(database):
    database.execute(
        "INSERT INTO personal_tasks (id, user_id, source_update_id, title, status, created_at_ms, updated_at_ms) "
        "VALUES (1, 100, 500, 'Submit lab', 'open', 1, 1)"
    )
    database.execute(
        "INSERT INTO personal_task_reminders (id, task_id, user_id, scheduled_at_ms, message_text, status, attempt_count, created_at_ms, updated_at_ms) "
        "VALUES (1, 1, 100, 9000, 'Submit lab', 'pending', 0, 1, 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO personal_task_reminders (id, task_id, user_id, scheduled_at_ms, message_text, status, attempt_count, created_at_ms, updated_at_ms) "
            "VALUES (2, 1, 100, 9000, 'duplicate', 'pending', 0, 1, 1)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO personal_tasks (id, user_id, title, status, created_at_ms, updated_at_ms) "
            "VALUES (2, 200, 'Bad state', 'sending', 1, 1)"
        )


def test_personal_task_records_are_separate_by_telegram_user(database):
    database.executemany(
        "INSERT INTO personal_tasks (user_id, source_update_id, title, status, created_at_ms, updated_at_ms) "
        "VALUES (?, ?, ?, 'open', 1, 1)",
        [(101, 1, "Read chapter"), (202, 2, "Revise algebra")],
    )

    rows = database.execute(
        "SELECT title FROM personal_tasks WHERE user_id = ? ORDER BY id", (101,)
    ).fetchall()
    assert [row[0] for row in rows] == ["Read chapter"]


def test_task_intent_parses_explicit_local_deadline_and_reminder():
    intent = service.validate_task_intent(
        {
            "action": "create_task",
            "title": "Submit lab",
            "due_local": "2026-11-10T17:00:00+03:00",
            "reminder_local": "2026-11-10T16:00:00+03:00",
        },
        "Africa/Addis_Ababa",
        1_794_000_000_000,
    )

    assert intent.action == "create_task"
    assert intent.title == "Submit lab"
    assert intent.reminder_at_ms < intent.due_at_ms


def test_task_intent_rejects_ambiguous_and_past_reminder_times():
    ambiguous = service.validate_task_intent(
        {"action": "create_task", "title": "Submit lab", "reminder_local": "2026-11-10T17:00:00"},
        "Africa/Addis_Ababa",
        1_794_000_000_000,
    )
    past = service.validate_task_intent(
        {"action": "create_task", "title": "Submit lab", "reminder_local": "2026-11-01T17:00:00+03:00"},
        "Africa/Addis_Ababa",
        1_794_000_000_000,
    )

    assert ambiguous.action == "clarification"
    assert past.action == "clarification"


def test_task_intent_rejects_a_model_offset_that_disagrees_with_user_timezone():
    intent = service.validate_task_intent(
        {"action": "create_task", "title": "Submit lab", "reminder_local": "2026-11-10T17:00:00-05:00"},
        "Africa/Addis_Ababa",
        1_794_000_000_000,
    )

    assert intent.action == "clarification"


def test_task_intent_leaves_ordinary_study_questions_for_tutor():
    intent = service.validate_task_intent(
        {"action": "not_task_request"}, "Africa/Addis_Ababa", 1_794_000_000_000
    )

    assert intent.action == "not_task_request"


def test_task_heuristic_only_classifies_clear_personal_task_language():
    assert service.looks_like_task_request("Remind me to submit the lab tomorrow")
    assert service.looks_like_task_request("Add this to my tasks: revise chapter 4")
    assert not service.looks_like_task_request("Explain task scheduling in operating systems")


def test_relative_reminder_after_two_minutes_is_handled_without_ai():
    class NeverCallAi:
        async def run(self, *_args, **_kwargs):
            raise AssertionError("relative reminder should be parsed deterministically")

    now_ms = 1_800_000_000_000
    intent = asyncio.run(service.parse_task_intent(
        NeverCallAi(), "Can u put a reminder for me after 2 minutes remind me",
        "Africa/Addis_Ababa", now_ms,
    ))

    assert intent.action == "create_task"
    assert intent.title == "Reminder"
    assert intent.reminder_at_ms == now_ms + 120_000


def test_parse_task_intent_uses_compact_json_only_workers_ai_request():
    binding = AsyncMock()
    binding.run.return_value = {
        "response": '{"action":"create_task","title":"Read chapter","reminder_local":"2026-11-10T17:00:00+03:00"}'
    }

    intent = asyncio.run(service.parse_task_intent(
        binding, "remind me to read chapter tomorrow at 5", "Africa/Addis_Ababa", 1_794_000_000_000
    ))

    assert intent.action == "create_task"
    model, payload = binding.run.call_args.args
    assert model == service.TASK_INTENT_MODEL
    assert payload["temperature"] == 0


def test_user_timezone_defaults_to_addis_and_accepts_valid_iana_zone(database):
    async def scenario():
        assert await service.get_user_timezone(database, 500) == "Africa/Addis_Ababa"
        assert await service.set_user_timezone(database, 500, "Europe/London", 10)
        assert await service.get_user_timezone(database, 500) == "Europe/London"
        assert not await service.set_user_timezone(database, 500, "Not/A_Zone", 11)
        assert await service.get_user_timezone(database, 500) == "Europe/London"

    asyncio.run(scenario())


def test_task_crud_is_owner_scoped_and_completion_cancels_reminder(database):
    async def scenario():
        intent = service.TaskIntent(
            action="create_task", title="Submit lab", due_at_ms=50_000, reminder_at_ms=40_000
        )
        task = await service.create_task(database, 100, 12, intent, "Africa/Addis_Ababa", 1_000)
        assert task and task["title"] == "Submit lab"
        replay = await service.create_task(database, 100, 12, intent, "Africa/Addis_Ababa", 2_000)
        assert replay["id"] == task["id"]
        assert await service.complete_task(database, 200, task["id"], 3_000) is False
        assert await service.complete_task(database, 100, task["id"], 3_000) is True
        reminder = await database.prepare(
            "SELECT status FROM personal_task_reminders WHERE task_id = ?"
        ).bind(task["id"]).first()
        assert reminder["status"] == "cancelled"
        assert await service.list_tasks(database, 200) == []

    asyncio.run(scenario())


def test_claiming_due_reminders_excludes_overlapping_claims_and_private_scope(database):
    async def scenario():
        task = await service.create_task(
            database, 100, 88,
            service.TaskIntent(action="create_task", title="Read chapter", reminder_at_ms=10_000),
            "Africa/Addis_Ababa", 1_000,
        )
        claimed = await service.claim_due_personal_task_reminders(database, 10_000)
        second_claim = await service.claim_due_personal_task_reminders(database, 10_001)
        assert len(claimed) == 1
        assert claimed[0]["user_id"] == 100
        assert second_claim == []
        assert task["id"] == claimed[0]["task_id"]

    asyncio.run(scenario())


def test_expired_send_becomes_uncertain_instead_of_being_resent(database):
    async def scenario():
        await service.create_task(
            database, 100, 99,
            service.TaskIntent(action="create_task", title="Read chapter", reminder_at_ms=1_000),
            "Africa/Addis_Ababa", 10,
        )
        await service.claim_due_personal_task_reminders(database, 1_000)
        claimed_after_expiry = await service.claim_due_personal_task_reminders(
            database, 1_000 + service.REMINDER_LEASE_MS
        )
        row = await database.prepare(
            "SELECT status FROM personal_task_reminders"
        ).first()
        assert claimed_after_expiry == []
        assert row["status"] == "uncertain"

    asyncio.run(scenario())


def test_delivery_distinguishes_definite_rejection_from_ambiguous_failure(database):
    async def scenario():
        await service.create_task(
            database, 100, 100,
            service.TaskIntent(action="create_task", title="Rejected", reminder_at_ms=1_000),
            "Africa/Addis_Ababa", 10,
        )
        await service.create_task(
            database, 101, 101,
            service.TaskIntent(action="create_task", title="Unknown", reminder_at_ms=1_000),
            "Africa/Addis_Ababa", 10,
        )
        claimed = await service.claim_due_personal_task_reminders(database, 1_000)

        async def send(reminder):
            if reminder["chat_id"] == "100":
                raise service.DefiniteDeliveryError()
            raise TimeoutError()

        await service.dispatch_claimed_personal_task_reminders(database, claimed, send, 1_000)
        states = await database.prepare(
            "SELECT user_id, status, retry_after_ms FROM personal_task_reminders ORDER BY user_id"
        ).all()
        assert states["results"][0]["status"] == "failed"
        assert states["results"][0]["retry_after_ms"] is not None
        assert states["results"][1]["status"] == "uncertain"

    asyncio.run(scenario())


def test_reschedule_rename_and_delete_task_are_owner_scoped(database):
    async def scenario():
        task = await service.create_task(
            database, 100, 222,
            service.TaskIntent(action="create_task", title="Old title", due_at_ms=20_000, reminder_at_ms=10_000),
            "Africa/Addis_Ababa", 1_000,
        )
        assert await service.reschedule_task(database, 200, task["id"], 30_000, 2_000) is False
        assert await service.reschedule_task(database, 100, task["id"], 30_000, 2_000) is True
        assert await service.rename_task(database, 100, task["id"], "  New   title ", 3_000) is True
        reminder = await database.prepare(
            "SELECT scheduled_at_ms, message_text FROM personal_task_reminders WHERE task_id = ?"
        ).bind(task["id"]).first()
        assert reminder == {"scheduled_at_ms": 30_000, "message_text": "New title"}
        assert await service.delete_task(database, 200, task["id"], 4_000) is False
        assert await service.delete_task(database, 100, task["id"], 4_000) is True
        assert await database.prepare(
            "SELECT id FROM personal_task_reminders WHERE task_id = ?"
        ).bind(task["id"]).first() is None

    asyncio.run(scenario())


def test_delete_all_tasks_removes_only_calling_users_data(database):
    async def scenario():
        for user_id, update_id in ((10, 1), (10, 2), (20, 3)):
            await service.create_task(
                database, user_id, update_id,
                service.TaskIntent(action="create_task", title=f"Task {update_id}"),
                "Africa/Addis_Ababa", 1_000,
            )
        deleted = await service.delete_all_tasks(database, 10, 2_000)
        assert deleted == 2
        assert await service.list_tasks(database, 10, "all") == []
        assert len(await service.list_tasks(database, 20, "all")) == 1

    asyncio.run(scenario())


def test_pending_clarification_expires_and_is_consumed_once(database):
    async def scenario():
        intent = service.TaskIntent(
            action="clarification", title="Submit lab", clarification="What time should I use?"
        )
        await service.save_pending_action(database, 50, 900, intent, 1_000)
        preview = await service.peek_pending_action(database, 50, 2_000)
        assert preview["title"] == "Submit lab"
        value = await service.consume_pending_action(database, 50, 2_000)
        assert value["title"] == "Submit lab"
        assert await service.consume_pending_action(database, 50, 2_001) is None
        await service.save_pending_action(database, 50, 901, intent, 5_000)
        assert await service.consume_pending_action(database, 50, 5_000 + 15 * 60_000) is None

    asyncio.run(scenario())
