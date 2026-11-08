import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class FakeDatabase:
    def __init__(self):
        self.runs = []
        self.claimed_reminders = []
        self.statement_results = {"results": self.claimed_reminders}
        self.first_results = []

    def prepare(self, query):
        return FakeStatement(self, query)


class FakeStatement:
    def __init__(self, database, query):
        self.database = database
        self.query = query

    def bind(self, *values):
        self.values = values
        return self

    async def run(self):
        self.database.runs.append((self.query, self.values))
        return {"results": self.database.claimed_reminders}

    async def all(self):
        self.database.runs.append((self.query, self.values))
        return self.database.statement_results

    async def first(self):
        self.database.runs.append((self.query, self.values))
        if self.database.first_results:
            return self.database.first_results.pop(0)
        return self.database.statement_results


class FakeWorkerEntrypoint:
    def __init__(self, env):
        self.env = env


def load_worker(monkeypatch):
    workers_stub = ModuleType("workers")
    workers_stub.WorkerEntrypoint = FakeWorkerEntrypoint
    monkeypatch.setitem(sys.modules, "workers", workers_stub)

    worker_path = Path(__file__).parents[1] / "src" / "worker.py"
    monkeypatch.syspath_prepend(str(worker_path.parent))
    spec = importlib.util.spec_from_file_location("cloudflare_worker_under_test", worker_path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


def test_scheduled_handler_uses_entrypoint_env_when_event_env_is_none(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    entrypoint = worker.Default(env=SimpleNamespace(DB=database))
    controller = SimpleNamespace(scheduledTime=1234)

    asyncio.run(entrypoint.scheduled(controller, None, None))

    assert database.runs[0] == (
        "UPDATE runtime_probe SET last_cron_at = ?, cron_runs = cron_runs + 1 WHERE id = ?",
        (1234, 1),
    )


def test_scheduled_handler_dispatches_personal_reminder_to_private_user_chat(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    calls = []

    async def claim(_database, now_ms):
        calls.append(("claim", now_ms))
        return [{"id": 3, "user_id": 77, "message_text": "Submit lab", "attempt_count": 1}]

    async def dispatch(_database, rows, send, now_ms):
        calls.append(("dispatch", rows, now_ms))
        await send(rows[0])

    async def send(row):
        calls.append(("send", row["chat_id"], row["message_text"]))

    async def private_send(_token, row):
        await send({"chat_id": str(row["user_id"]), "message_text": row["message_text"]})

    monkeypatch.setattr(worker, "claim_due_reminders", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker, "dispatch_claimed_reminders", AsyncMock())
    monkeypatch.setattr(worker.personal_task_service, "claim_due_personal_task_reminders", claim)
    monkeypatch.setattr(worker.personal_task_service, "dispatch_claimed_personal_task_reminders", dispatch)
    monkeypatch.setattr(worker, "_send_personal_task_reminder", private_send)
    monkeypatch.setattr(worker, "enqueue_weekly_digests", AsyncMock())
    monkeypatch.setattr(worker, "claim_due_weekly_digests", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker, "dispatch_claimed_weekly_digests", AsyncMock())
    env = SimpleNamespace(DB=database, BOT_TOKEN="token", AI=None)

    asyncio.run(worker.Default(env).scheduled(SimpleNamespace(scheduledTime=50_000), env, None))

    assert ("claim", 50_000) in calls
    assert ("send", "77", "Submit lab") in calls


def test_personal_reminder_failure_does_not_skip_class_digest_tick(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    digest = AsyncMock()

    monkeypatch.setattr(worker, "claim_due_reminders", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker, "dispatch_claimed_reminders", AsyncMock())
    monkeypatch.setattr(worker.personal_task_service, "claim_due_personal_task_reminders", AsyncMock(side_effect=RuntimeError("D1 unavailable")))
    monkeypatch.setattr(worker, "enqueue_weekly_digests", digest)
    monkeypatch.setattr(worker, "claim_due_weekly_digests", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker, "dispatch_claimed_weekly_digests", AsyncMock())
    env = SimpleNamespace(DB=database, BOT_TOKEN="token", AI=None)

    asyncio.run(worker.Default(env).scheduled(SimpleNamespace(scheduledTime=50_000), env, None))

    digest.assert_awaited_once_with(database, 50_000)


def test_due_reminder_claim_returns_rows_from_atomic_d1_update(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    due_reminder = {
        "id": 7,
        "chat_id": "-100123",
        "thread_id": 42,
        "message_text": "Assignment due tomorrow",
        "attempt_count": 1,
    }
    database.claimed_reminders = [due_reminder]

    claimed = asyncio.run(worker.claim_due_reminders(database, 10_000))

    assert claimed == [due_reminder]
    query, values = database.runs[0]
    assert query.strip().startswith("UPDATE reminder_outbox")
    assert "RETURNING id, chat_id, thread_id, message_text, attempt_count" in query
    assert values[0] == 70_000
    assert values[1] == 10_000


def test_dispatch_marks_sent_after_telegram_confirms(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    reminder = {
        "id": 7,
        "chat_id": "-100123",
        "thread_id": 42,
        "message_text": "Assignment due tomorrow",
        "attempt_count": 1,
    }
    sent = []

    async def send_message(row):
        sent.append(row)

    asyncio.run(worker.dispatch_claimed_reminders(database, [reminder], send_message, 20_000))

    assert sent == [reminder]
    assert database.runs == [
        (
            "UPDATE reminder_outbox SET status = 'sent', sent_at_ms = ?, lease_until_ms = NULL, last_error = NULL WHERE id = ? AND status = 'sending'",
            (20_000, 7),
        )
    ]


def test_dispatch_schedules_bounded_retry_after_send_failure(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    reminder = {
        "id": 9,
        "chat_id": "-100123",
        "thread_id": None,
        "message_text": "Exam tomorrow",
        "attempt_count": 2,
    }

    async def fail_send(row):
        raise RuntimeError("provider unavailable")

    asyncio.run(worker.dispatch_claimed_reminders(database, [reminder], fail_send, 20_000))

    query, values = database.runs[0]
    assert query.startswith("UPDATE reminder_outbox SET status = 'failed'")
    assert values == (80_000, "telegram_send_failed", 9)


def test_telegram_transport_posts_threaded_message_and_checks_provider_result(monkeypatch):
    worker = load_worker(monkeypatch)
    calls = []

    class FakeResponse:
        status = 200

        async def json(self):
            return {"ok": True}

    async def fake_fetch(url, **options):
        calls.append((url, options))
        return FakeResponse()

    sys.modules["workers"].fetch = fake_fetch
    reminder = {
        "chat_id": "-100123",
        "thread_id": 42,
        "message_text": "Assignment due tomorrow",
    }

    asyncio.run(worker.send_telegram_message("test-token", reminder))

    url, options = calls[0]
    assert url == "https://api.telegram.org/bottest-token/sendMessage"
    assert options["method"] == "POST"
    assert json.loads(options["body"]) == {
        "chat_id": "-100123",
        "message_thread_id": 42,
        "text": "Assignment due tomorrow",
        "parse_mode": "HTML",
    }


def test_send_telegram_message_draft_uses_telegram_progressive_draft_api(monkeypatch):
    worker = load_worker(monkeypatch)
    calls = []

    class FakeResponse:
        status = 200

        async def json(self):
            return {"ok": True, "result": True}

    async def fake_fetch(url, **options):
        calls.append((url, options))
        return FakeResponse()

    sys.modules["workers"].fetch = fake_fetch
    asyncio.run(worker.send_telegram_message_draft(
        "test-token", "77", 654321, "<b>Working</b>", thread_id=12
    ))

    assert calls[0][0] == "https://api.telegram.org/bottest-token/sendMessageDraft"
    assert json.loads(calls[0][1]["body"]) == {
        "chat_id": "77",
        "draft_id": 654321,
        "text": "<b>Working</b>",
        "parse_mode": "HTML",
        "message_thread_id": 12,
    }


def test_tutor_stream_callback_does_not_wait_for_telegram_preview_request(monkeypatch):
    worker = load_worker(monkeypatch)
    sent = []

    async def scenario():
        gate = asyncio.Event()

        async def fake_send(_token, _chat_id, _draft_id, text, _thread_id):
            sent.append(text)
            if len(sent) == 1:
                await gate.wait()

        monkeypatch.setattr(worker, "send_telegram_message_draft", fake_send)
        draft = worker.TelegramTutorDraft("token", "77", 12)
        draft.active = True

        await draft.on_partial("First preview")
        await asyncio.sleep(0)
        assert sent == ["First preview"]

        await draft.on_partial("Second preview " + ("more detail " * 10))
        gate.set()
        await draft.finish()

    asyncio.run(scenario())

    assert sent[0] == "First preview"
    assert sent[1].startswith("Second preview")


def test_ask_command_passes_question_and_course_topic_context_to_tutor(monkeypatch):
    worker = load_worker(monkeypatch)
    calls = []

    async def answer_course_question(database, ai_binding, search_key, chat_id, thread_id, user_id, question, now_ms, **_kwargs):
        calls.append((database, ai_binding, search_key, chat_id, thread_id, user_id, question, now_ms))
        return "📘 I found this in your course materials."

    worker.course_tutor_service.answer_course_question = answer_course_question
    database = FakeDatabase()
    message = {
        "message_thread_id": 42,
        "from": {"id": 77},
    }
    chat = {"id": -100123, "type": "supergroup"}
    ai_binding = object()

    reply = asyncio.run(worker._course_tutor_command(
        database, message, chat, "-100123", "/ask Explain normalization", 1234,
        ai_binding, "tavily-secret",
    ))

    assert reply == "📘 I found this in your course materials."
    assert calls == [(database, ai_binding, "tavily-secret", "-100123", 42, 77, "Explain normalization", 1234)]


def test_ask_in_private_uses_general_tutor(monkeypatch):
    worker = load_worker(monkeypatch)
    calls = []

    async def answer_general_question(*args, **kwargs):
        calls.append((args, kwargs))
        return "🌐 General tutor answer."

    worker.course_tutor_service.answer_general_question = answer_general_question

    ai_binding = object()
    reply = asyncio.run(worker._course_tutor_command(
        FakeDatabase(), {"from": {"id": 77}}, {"id": 77, "type": "private"}, "77",
        "/ask Explain normalization", 1234, ai_binding, "tavily-secret",
    ))

    assert reply == "🌐 General tutor answer."
    assert len(calls) == 1
    assert calls[0][0][1:] == (ai_binding, "tavily-secret", "77", 77, "Explain normalization", 1234)


def test_plain_private_text_uses_general_tutor(monkeypatch):
    worker = load_worker(monkeypatch)
    calls = []

    async def answer_general_question(*args, **kwargs):
        calls.append((args, kwargs))
        return "Tutor answer"

    worker.course_tutor_service.answer_general_question = answer_general_question
    ai_binding = object()
    reply = asyncio.run(worker._process_message_update(
        FakeDatabase(), "token", 10,
        {"chat": {"id": 77, "type": "private"}, "from": {"id": 77}, "text": "Explain recursion"},
        1234, "", "", "academicgroupmanagementbot", ai_binding, "",
    ))

    assert reply == "Tutor answer"
    assert calls[0][0][1:] == (ai_binding, "", "77", 77, "Explain recursion", 1234)


def test_private_reminder_request_is_handled_before_the_tutor(monkeypatch):
    worker = load_worker(monkeypatch)
    intent = worker.personal_task_service.TaskIntent(
        action="create_task", title="Submit lab", reminder_at_ms=1_800_000_000_000
    )
    monkeypatch.setattr(worker.personal_task_service, "get_user_timezone", AsyncMock(return_value="Africa/Addis_Ababa"))
    monkeypatch.setattr(worker.personal_task_service, "parse_task_intent", AsyncMock(return_value=intent))
    monkeypatch.setattr(worker.personal_task_service, "create_task", AsyncMock(return_value={"id": 42, "title": "Submit lab"}))
    monkeypatch.setattr(worker.course_tutor_service, "answer_general_question", AsyncMock(side_effect=AssertionError("task routed to tutor")))
    monkeypatch.setattr(worker, "_save_resource_update", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(worker, "_private_tutor_attachment_command", AsyncMock(return_value=None))

    reply = asyncio.run(worker._process_message_update(
        FakeDatabase(), "token", 77,
        {"chat": {"id": 77, "type": "private"}, "from": {"id": 77}, "text": "Remind me to submit lab tomorrow at 5"},
        1_799_000_000_000, "", "", "academicgroupmanagementbot", object(), "",
    ))

    assert "Submit lab" in reply
    assert "Africa/Addis_Ababa" in reply
    worker.personal_task_service.create_task.assert_awaited_once()


def test_private_group_setup_request_returns_bot_linking_steps_without_tutor(monkeypatch):
    worker = load_worker(monkeypatch)
    monkeypatch.setattr(worker, "_save_resource_update", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(worker, "_private_tutor_attachment_command", AsyncMock(return_value=None))
    tutor = AsyncMock(side_effect=AssertionError("group setup request should be routed directly"))
    monkeypatch.setattr(worker.course_tutor_service, "answer_general_question", tutor)

    reply = asyncio.run(worker._process_message_update(
        FakeDatabase(), "token", 81,
        {"chat": {"id": 77, "type": "private"}, "from": {"id": 77}, "text": "How do I set up my group?"},
        1234, "", "", "academicgroupmanagementbot", object(), "",
    ))

    assert "/group_id" in reply
    assert "/group_setup" in reply
    assert "COLLEGE_INVITE_CODE" in reply
    tutor.assert_not_awaited()


def test_contextual_link_it_to_you_resolves_against_recent_group_setup(monkeypatch):
    worker = load_worker(monkeypatch)
    monkeypatch.setattr(worker.course_tutor_service, "_read_tutor_context", AsyncMock(return_value=("", [
        {"role": "user", "content": "How do I set up my group?"},
        {"role": "assistant", "content": "Here are generic Telegram group creation steps."},
    ])))

    result = asyncio.run(worker._is_group_linking_request(
        FakeDatabase(), {"from": {"id": 77}}, "77", "Link it to u"
    ))

    assert result is True


def test_regular_study_question_skips_task_intent_model(monkeypatch):
    worker = load_worker(monkeypatch)
    parser = AsyncMock(side_effect=AssertionError("ordinary question should skip task classifier"))
    monkeypatch.setattr(worker.personal_task_service, "parse_task_intent", parser)
    answer = AsyncMock(return_value="Tutor answer")
    monkeypatch.setattr(worker.course_tutor_service, "answer_general_question", answer)
    monkeypatch.setattr(worker, "_save_resource_update", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(worker, "_private_tutor_attachment_command", AsyncMock(return_value=None))

    reply = asyncio.run(worker._process_message_update(
        FakeDatabase(), "token", 78,
        {"chat": {"id": 77, "type": "private"}, "from": {"id": 77}, "text": "Explain recursion"},
        1234, "", "", "academicgroupmanagementbot", object(), "",
    ))

    assert reply == "Tutor answer"
    parser.assert_not_awaited()


def test_task_list_command_is_private_and_returns_inline_actions(monkeypatch):
    worker = load_worker(monkeypatch)
    monkeypatch.setattr(worker.personal_task_service, "list_tasks", AsyncMock(return_value=[{
        "id": 42, "title": "Submit lab", "due_at_ms": 1_800_000_000_000, "status": "open",
    }]))
    monkeypatch.setattr(worker.personal_task_service, "get_user_timezone", AsyncMock(return_value="Africa/Addis_Ababa"))
    monkeypatch.setattr(worker, "_save_resource_update", AsyncMock(return_value=(False, None)))

    reply = asyncio.run(worker._process_message_update(
        FakeDatabase(), "token", 79,
        {"chat": {"id": 77, "type": "private"}, "from": {"id": 77}, "text": "/tasks"},
        1234, "", "", "academicgroupmanagementbot", object(), "",
    ))

    assert isinstance(reply, worker.PersonalTaskReply)
    assert "Submit lab" in reply.text
    actions = [button["callback_data"] for row in reply.reply_markup["inline_keyboard"] for button in row]
    assert "pt:c:42" in actions
    assert "pt:r:42" in actions


def test_personal_task_callback_checks_owner_before_mutating(monkeypatch):
    worker = load_worker(monkeypatch)
    calls = []

    async def complete(_database, user_id, task_id, now_ms):
        calls.append((user_id, task_id, now_ms))
        return False

    sent = []
    monkeypatch.setattr(worker.personal_task_service, "complete_task", complete)
    monkeypatch.setattr(worker, "answer_telegram_callback", AsyncMock())
    monkeypatch.setattr(worker, "send_telegram_message", AsyncMock(side_effect=lambda _token, payload: sent.append(payload)))
    result = asyncio.run(worker._handle_personal_task_callback_update(
        FakeDatabase(), "token", {
            "callback_query": {
                "id": "cb1", "from": {"id": 99}, "data": "pt:c:42",
                "message": {"chat": {"id": 77, "type": "private"}},
            }
        }, 1234,
    ))

    assert result == "__QUIZ_HANDLED__"
    assert calls == []
    worker.answer_telegram_callback.assert_awaited_once_with(
        "token", "cb1", "Open the bot privately to manage your tasks."
    )
    assert sent == []


def test_tutor_memory_commands_save_show_and_forget_only_calling_user(monkeypatch):
    worker = load_worker(monkeypatch)
    calls = []

    async def save(_database, user_id, preference, now_ms):
        calls.append(("save", user_id, preference, now_ms))

    async def read(_database, user_id):
        calls.append(("read", user_id))
        return "Use examples & short steps"

    async def forget(_database, user_id):
        calls.append(("forget", user_id))

    monkeypatch.setattr(worker.course_tutor_service, "set_tutor_preferences", save)
    monkeypatch.setattr(worker.course_tutor_service, "get_tutor_preferences", read)
    monkeypatch.setattr(worker.course_tutor_service, "forget_tutor_user", forget)
    database = FakeDatabase()
    message = {"from": {"id": 77}}

    saved = asyncio.run(worker._tutor_memory_command(
        database, message, {"type": "private"}, "/remember use examples & short steps", 1234
    ))
    shown = asyncio.run(worker._tutor_memory_command(
        database, message, {"type": "private"}, "/memory", 1235
    ))
    erased = asyncio.run(worker._tutor_memory_command(
        database, message, {"type": "supergroup"}, "/forget_me", 1236
    ))

    assert saved == "Tutor preference saved for your account."
    assert "Use examples &amp; short steps" in shown
    assert erased == "Your saved tutor conversations and preferences have been erased."
    assert calls == [
        ("save", 77, "use examples & short steps", 1234),
        ("read", 77),
        ("forget", 77),
    ]


def test_tutor_memory_command_hides_preference_in_groups(monkeypatch):
    worker = load_worker(monkeypatch)

    async def unexpected_read(*_args):
        raise AssertionError("group /memory must not read or reveal personal data")

    monkeypatch.setattr(worker.course_tutor_service, "get_tutor_preferences", unexpected_read)
    reply = asyncio.run(worker._tutor_memory_command(
        FakeDatabase(), {"from": {"id": 77}}, {"type": "group"}, "/memory", 1234
    ))
    assert "privately" in reply


def test_process_message_update_routes_tutor_memory_command_before_chatbot(monkeypatch):
    worker = load_worker(monkeypatch)
    saved = []

    async def save(_database, user_id, preference, now_ms):
        saved.append((user_id, preference, now_ms))

    async def resource_update(*_args):
        return False, None

    monkeypatch.setattr(worker.course_tutor_service, "set_tutor_preferences", save)
    monkeypatch.setattr(worker, "_save_resource_update", resource_update)
    reply = asyncio.run(worker._process_message_update(
        FakeDatabase(), "token", 10,
        {"chat": {"id": 77, "type": "private"}, "from": {"id": 77}, "text": "/remember use worked examples"},
        1234, "", "", "academicgroupmanagementbot", object(), "",
    ))

    assert reply == "Tutor preference saved for your account."
    assert saved == [(77, "use worked examples", 1234)]


def test_academic_item_sync_upserts_item_and_reminder_rows(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.first_results = [{"college_id": "college-1"}, None]
    item = worker.AcademicItemSync(
        source_item_id=123,
        group_id=8,
        chat_id="-100123",
        thread_id=42,
        item_type="assignment",
        title="Lab <report>",
        course_name="Database Systems",
        deadline_ms=1_799_025_600_000,
        room="B-12",
        coverage="Chapter 2 & 3",
        reminders=[worker.ReminderSync(source_reminder_id=456, send_time_ms=1_799_000_000_000)],
    )

    synced = asyncio.run(worker.sync_academic_items(database, [item]))

    assert synced == 1
    assert len(database.runs) == 5
    academic_sql, academic_values = database.runs[3]
    assert "INSERT INTO academic_items" in academic_sql
    assert "ON CONFLICT(source_item_id) DO UPDATE" in academic_sql
    assert academic_values[:10] == (
        123,
        8,
        "-100123",
        42,
        "assignment",
        "Lab <report>",
        1_799_025_600_000,
        "B-12",
        "Chapter 2 & 3",
        "active",
    )
    assert academic_values[11:] == ("Database Systems", None, "college-1", None)

    reminder_sql, reminder_values = database.runs[4]
    assert "ON CONFLICT(source_item_id, source_reminder_id) DO UPDATE" in reminder_sql
    assert reminder_values[:7] == (123, 456, 8, "-100123", 42, 1_799_000_000_000, reminder_values[6])
    assert "Lab &lt;report&gt;" in reminder_values[6]
    assert "Chapter 2 &amp; 3" in reminder_values[6]


def test_academic_reminder_sync_preserves_cancelled_flag_and_addis_format(monkeypatch):
    worker = load_worker(monkeypatch)
    item = worker.AcademicItemSync(
        source_item_id=123,
        group_id=8,
        chat_id="-100123",
        item_type="exam",
        title="Final",
        deadline_ms=1_799_025_600_000,
        reminders=[
            worker.ReminderSync(
                source_reminder_id=456,
                send_time_ms=1_799_000_000_000,
                sent=False,
                cancelled=True,
            )
        ],
    )
    database = FakeDatabase()
    database.first_results = [{"college_id": "college-1"}]

    asyncio.run(worker.sync_academic_items(database, [item]))

    assert database.runs[2][1][7] == "cancelled"
    assert " (Addis Ababa Time)" in database.runs[2][1][6]


def test_academic_reminder_sync_does_not_requeue_already_sent_reminder(monkeypatch):
    worker = load_worker(monkeypatch)
    item = worker.AcademicItemSync(
        source_item_id=123,
        group_id=8,
        chat_id="-100123",
        item_type="exam",
        reminders=[
            worker.ReminderSync(
                source_reminder_id=456,
                send_time_ms=1_799_000_000_000,
                sent=True,
            )
        ],
    )
    database = FakeDatabase()
    database.first_results = [{"college_id": "college-1"}]

    asyncio.run(worker.sync_academic_items(database, [item]))

    assert database.runs[2][1][7] == "sent"


def test_academic_sync_endpoint_requires_shared_secret(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    request = SimpleNamespace(scope={"env": SimpleNamespace(DB=database, ACADEMIC_SYNC_SECRET="test-secret")})
    payload = worker.AcademicItemSyncBatch(
        items=[
            worker.AcademicItemSync(
                source_item_id=123,
                group_id=8,
                chat_id="-100123",
                item_type="quiz",
            )
        ]
    )

    with pytest.raises(worker.HTTPException) as error:
        asyncio.run(worker.sync_items(payload, request, "wrong-secret"))
    assert getattr(error.value, "status_code", None) == 403

    assert database.runs == []


def test_college_provisioning_stores_only_hashed_invite_code(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    request = SimpleNamespace(
        scope={"env": SimpleNamespace(DB=database, ACADEMIC_SYNC_SECRET="test-secret")}
    )
    invite_code = "random-code-123456"
    payload = worker.CollegeProvision(
        college_id="sample-college",
        college_name="Sample College",
        invite_code=invite_code,
        remaining_uses=10,
    )

    response = asyncio.run(worker.provision_college(payload, request, "test-secret"))

    assert response.status_code == 200
    assert len(database.runs) == 2
    college_sql, college_values = database.runs[0]
    assert "INSERT INTO colleges" in college_sql
    assert college_values[:2] == ("sample-college", "Sample College")
    invite_sql, invite_values = database.runs[1]
    assert "INSERT INTO college_invite_codes" in invite_sql
    assert invite_values[0] == worker._invite_code_hash(invite_code)
    assert invite_values[0] != invite_code


def test_academic_command_parses_addis_deadline_and_keeps_course_title(monkeypatch):
    worker = load_worker(monkeypatch)

    parsed = worker.parse_academic_command(
        "/assignment Operating Systems HW 3 | 2026-11-05 14:30"
    )

    assert parsed == {
        "item_type": "assignment",
        "title": "Operating Systems HW 3",
        "deadline_ms": 1_793_878_200_000,
    }


def test_academic_command_rejects_ambiguous_deadline(monkeypatch):
    worker = load_worker(monkeypatch)

    assert worker.parse_academic_command("/exam Final exam | next Friday") is None


def test_telegram_group_ids_can_be_negative_in_d1_records(monkeypatch):
    worker = load_worker(monkeypatch)

    item = worker.AcademicItemSync(
        source_item_id=900,
        group_id=-100123456789,
        chat_id="-100123456789",
        item_type="exam",
    )

    assert item.group_id == -100123456789


def test_reminder_schedule_uses_only_future_addis_offsets(monkeypatch):
    worker = load_worker(monkeypatch)
    deadline_ms = 1_793_878_200_000
    now_ms = deadline_ms - 50 * 60 * 60 * 1000

    reminders = worker.build_reminder_schedule(900, deadline_ms, now_ms)

    assert [reminder["send_time_ms"] for reminder in reminders] == [
        deadline_ms - 48 * 60 * 60 * 1000,
        deadline_ms - 24 * 60 * 60 * 1000,
        deadline_ms,
    ]
    assert len({reminder["source_reminder_id"] for reminder in reminders}) == 3


def test_telegram_webhook_rejects_wrong_secret_before_parsing_update(monkeypatch):
    worker = load_worker(monkeypatch)

    class FakeRequest:
        async def body(self):
            raise AssertionError("Body should not be read before secret validation")

    response = asyncio.run(
        worker._read_authenticated_update(FakeRequest(), "expected-secret", "wrong-secret")
    )

    assert response.status_code == 403


def test_typing_action_uses_telegram_chat_action_and_topic(monkeypatch):
    worker = load_worker(monkeypatch)
    calls = []

    class Response:
        status = 200

        async def json(self):
            return {"ok": True}

    async def fetch(url, **options):
        calls.append((url, options))
        return Response()

    sys.modules["workers"].fetch = fetch
    asyncio.run(worker.send_telegram_typing_action("test-token", "-100123", 42))

    assert calls[0][0] == "https://api.telegram.org/bottest-token/sendChatAction"
    assert json.loads(calls[0][1]["body"]) == {
        "chat_id": "-100123", "action": "typing", "message_thread_id": 42
    }


def test_private_attachment_reaches_tutor_after_file_extraction(monkeypatch):
    worker = load_worker(monkeypatch)
    extracted = []
    answered = []

    async def extract(_token, _ai_binding, source):
        extracted.append(source)
        return "Extracted lecture text"

    async def answer(*args, **kwargs):
        answered.append((args, kwargs))
        return "Here is the summary."

    monkeypatch.setattr(worker.course_quiz_service, "extract_telegram_file_source", extract)
    monkeypatch.setattr(worker.course_tutor_service, "answer_general_question", answer)
    reply = asyncio.run(worker._private_tutor_attachment_command(
        FakeDatabase(),
        "test-token",
        {
            "from": {"id": 7},
            "chat": {"id": 77, "type": "private"},
            "document": {"file_id": "file-1", "file_name": "lecture.pdf", "mime_type": "application/pdf"},
            "caption": "Summarise this",
        },
        {"id": 77, "type": "private"},
        "77",
        "Summarise this",
        1_800_000_000_000,
        object(),
        "search-key",
    ))

    assert reply.endswith("Here is the summary.")
    assert extracted[0]["title"] == "lecture.pdf"
    assert answered[0][0][5] == "Summarise this"
    assert answered[0][1]["attachment_context"] == {
        "name": "lecture.pdf", "content": "Extracted lecture text",
    }


def test_private_attachment_uses_underlying_ai_binding_for_markdown(monkeypatch):
    worker = load_worker(monkeypatch)
    raw_binding = object()
    progressive_binding = worker.ProgressiveTutorBinding(raw_binding, lambda _text: None)
    extracted_with = []
    answered_with = []

    async def extract(_token, binding, _source):
        extracted_with.append(binding)
        return "Extracted lecture text"

    async def answer(*args, **kwargs):
        answered_with.append(args[1])
        return "Here is the summary."

    monkeypatch.setattr(worker.course_quiz_service, "extract_telegram_file_source", extract)
    monkeypatch.setattr(worker.course_tutor_service, "answer_general_question", answer)
    asyncio.run(worker._private_tutor_attachment_command(
        FakeDatabase(),
        "test-token",
        {
            "from": {"id": 7},
            "chat": {"id": 77, "type": "private"},
            "document": {"file_id": "file-1", "file_name": "lecture.pdf", "mime_type": "application/pdf"},
            "caption": "Summarise this",
        },
        {"id": 77, "type": "private"},
        "77",
        "Summarise this",
        1_800_000_000_000,
        progressive_binding,
        "search-key",
    ))

    assert extracted_with == [raw_binding]
    assert answered_with == [progressive_binding]


def test_long_telegram_html_splits_into_complete_messages(monkeypatch):
    worker = load_worker(monkeypatch)
    text = "<b>Lesson</b>\n\n<b>" + ("clear explanation " * 100) + "</b>"

    chunks = worker.split_telegram_html(text, max_length=300)

    assert len(chunks) > 1
    assert all(len(chunk) <= 300 for chunk in chunks)
    assert all(chunk.count("<b>") == chunk.count("</b>") for chunk in chunks)
    plain = "".join(__import__("re").sub(r"<[^>]+>", "", chunk) for chunk in chunks)
    assert plain.replace("📖 Continued\n\n", "").replace("\n\n", "") == text.replace("<b>", "").replace("</b>", "").replace("\n\n", "")


def test_telegram_update_claim_uses_lease_and_retryable_states(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.claimed_reminders = [{"update_id": 77}]

    claimed = asyncio.run(worker.claim_telegram_update(database, 77, 50_000))

    assert claimed is True
    query, values = database.runs[0]
    assert "INSERT INTO telegram_update_receipts" in query
    assert "status = 'failed'" in query
    assert "lease_until_ms <= ?" in query
    assert "RETURNING update_id" in query
    assert values == (77, 110_000, 50_000, 50_000)


def test_worker_item_writer_persists_group_topic_and_outbox_schedule(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.first_results = [{"college_id": "college-1"}, None]
    deadline_ms = 1_800_000_000_000
    message = {
        "message_id": 77,
        "message_thread_id": 42,
        "chat": {"id": -100123456789, "type": "supergroup"},
    }
    item = {
        "item_type": "exam",
        "title": "Midterm",
        "deadline_ms": deadline_ms,
    }

    asyncio.run(
        worker._save_telegram_academic_item(
            database,
            update_id=88,
            message=message,
            chat_id="-100123456789",
            item=item,
            now_ms=deadline_ms - 60 * 60 * 60 * 1000,
        )
    )

    assert len(database.runs) == 6  # group and topic lookup, academic item, three reminders
    academic_sql, academic_values = database.runs[2]
    assert "source_message_link" in academic_sql
    assert academic_values[1:4] == (-100123456789, "-100123456789", 42)
    assert academic_values[11] == "https://t.me/c/123456789/77"
    for query, values in database.runs[3:]:
        assert "INSERT INTO reminder_outbox" in query
        assert values[2:5] == (-100123456789, "-100123456789", 42)


def test_ai_extraction_parser_marks_high_confidence_items_active(monkeypatch):
    worker = load_worker(monkeypatch)

    parsed = worker.parse_ai_academic_extraction(
        {
            "type": "assignment",
            "title": "Database HW 2",
            "course": "Database Systems",
            "deadline": "2026-11-05T14:30:00",
            "room": None,
            "coverage": None,
            "confidence": 0.94,
        }
    )

    assert parsed["item_type"] == "assignment"
    assert parsed["title"] == "Database HW 2"
    assert parsed["course_name"] == "Database Systems"
    assert parsed["status"] == "active"
    assert parsed["deadline_ms"] == 1_793_878_200_000


def test_ai_extraction_parser_keeps_low_confidence_items_for_review(monkeypatch):
    worker = load_worker(monkeypatch)

    parsed = worker.parse_ai_academic_extraction(
        {
            "type": "exam",
            "title": "Maybe midterm",
            "deadline": None,
            "confidence": 0.78,
        }
    )

    assert parsed["status"] == "new"
    assert parsed["deadline_ms"] is None


def test_ai_extraction_parser_ignores_discussion_and_invalid_deadlines(monkeypatch):
    worker = load_worker(monkeypatch)

    assert worker.parse_ai_academic_extraction(
        {"type": "discussion", "title": "When is the exam?", "confidence": 0.99}
    ) is None
    assert worker.parse_ai_academic_extraction([]) is None
    assert worker.parse_ai_academic_extraction(
        {
            "type": "exam",
            "title": "Final",
            "confidence": 0.9,
            "coverage": {"includes": 42},
        }
    )["coverage"] is None


def test_academic_detection_gate_skips_chatter_and_keeps_academic_signals(monkeypatch):
    worker = load_worker(monkeypatch)

    assert worker.should_extract_academic_message("Anyone coming to campus today?") is False
    assert worker.should_extract_academic_message("Our exam is on Friday morning") is True
    assert worker.should_extract_academic_message("የፈተና ሽፋን ምዕራፍ ሶስት") is True


def test_ordinary_group_message_uses_ai_extraction_and_marks_review(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.first_results = [{"college_id": "college-1"}, None]

    async def fake_extract(api_key, text, now_ms, model):
        return {
            "item_type": "exam",
            "title": "Midterm",
            "course_name": "Database Systems",
            "deadline_ms": 1_800_000_000_000,
            "room": None,
            "coverage": None,
            "confidence": 0.78,
            "status": "new",
        }

    monkeypatch.setattr(worker, "extract_academic_event", fake_extract)
    update = {
        "message": {
            "message_id": 77,
            "message_thread_id": 42,
            "chat": {"id": -100123456789, "type": "supergroup"},
            "from": {"id": 5},
            "text": "The exam is next Friday",
        }
    }

    response = asyncio.run(
        worker._process_telegram_update(
            database,
            "test-token",
            88,
            update,
            1_700_000_000_000,
            "test-ai-key",
        )
    )

    assert "needs review" in response
    assert "/approve 88" in response
    assert len(database.runs) == 4  # group, topic, course-name lookup, and pending suggestion


def test_groq_extraction_uses_workers_fetch_and_validates_json(monkeypatch):
    worker = load_worker(monkeypatch)
    calls = []

    class FakeResponse:
        status = 200

        async def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "type": "assignment",
                                    "title": "OS Project",
                                    "course": "Operating Systems",
                                    "deadline": "2026-11-05T14:30:00",
                                    "confidence": 0.91,
                                }
                            )
                        }
                    }
                ]
            }

    async def fake_fetch(url, **options):
        calls.append((url, options))
        return FakeResponse()

    sys.modules["workers"].fetch = fake_fetch
    result = asyncio.run(
        worker.extract_academic_event(
            "test-ai-key",
            "Operating systems assignment due Thursday",
            1_793_000_000_000,
            "llama-3.1-8b-instant",
        )
    )

    assert result["status"] == "active"
    assert result["course_name"] == "Operating Systems"
    assert calls[0][0] == "https://api.groq.com/openai/v1/chat/completions"
    assert calls[0][1]["headers"]["authorization"] == "Bearer test-ai-key"


def test_groq_extraction_skips_chatter_without_calling_provider(monkeypatch):
    worker = load_worker(monkeypatch)

    async def unexpected_fetch(*args, **kwargs):
        raise AssertionError("chatter must not call the extraction provider")

    sys.modules["workers"].fetch = unexpected_fetch

    result = asyncio.run(
        worker.extract_academic_event(
            "test-ai-key",
            "Anyone coming to campus today?",
            1_793_000_000_000,
            "llama-3.1-8b-instant",
        )
    )

    assert result is None


def test_resource_extraction_captures_telegram_file_and_source_context(monkeypatch):
    worker = load_worker(monkeypatch)
    update = {
        "message": {
            "message_id": 81,
            "message_thread_id": 42,
            "chat": {"id": -100123456789, "type": "supergroup"},
            "document": {"file_id": "telegram-file-id", "file_name": "week-3-notes.pdf"},
            "caption": "Operating systems lecture notes",
        }
    }

    resource = worker.extract_telegram_resource(update["message"])

    assert resource == {
        "source_message_id": 81,
        "message_link": "https://t.me/c/123456789/81",
        "thread_id": 42,
        "resource_type": "document",
        "title": "week-3-notes.pdf",
        "caption": "Operating systems lecture notes",
        "file_id": "telegram-file-id",
        "mime_type": None,
        "file_size": None,
        "url": None,
    }


def test_resource_extraction_records_external_links_and_ignores_chatter(monkeypatch):
    worker = load_worker(monkeypatch)
    linked = worker.extract_telegram_resource(
        {
            "message_id": 82,
            "chat": {"id": -100123456789, "type": "supergroup"},
            "text": "Read https://example.edu/notes before class",
        }
    )
    chatter = worker.extract_telegram_resource(
        {
            "message_id": 83,
            "chat": {"id": -100123456789, "type": "supergroup"},
            "text": "Anyone online?",
        }
    )

    assert linked["url"] == "https://example.edu/notes"
    assert linked["resource_type"] == "link"
    assert chatter is None


def test_resource_search_is_scoped_to_group_and_optional_topic(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.statement_results = {
        "results": [
            {
                "title": "Week 3 notes.pdf",
                "caption": "Operating systems lecture",
                "resource_type": "document",
                "message_link": "https://t.me/c/123456789/81",
                "thread_id": 42,
            }
        ]
    }

    asyncio.run(worker.search_group_resources(database, "-100123456789", "operating", 42))

    query, values = database.runs[0]
    assert "chat_id = ?" in query
    assert "thread_id = ?" in query
    assert "LIKE ?" in query
    assert values == ("-100123456789", 42, "%operating%", "%operating%", 10)


def test_group_file_update_is_persisted_as_resource_without_downloading_file(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.first_results = [{"college_id": "college-1"}, None]
    update = {
        "message": {
            "message_id": 84,
            "message_thread_id": 42,
            "chat": {"id": -100123456789, "type": "supergroup"},
            "document": {"file_id": "telegram-file-id", "file_name": "lecture.pdf"},
        }
    }

    response = asyncio.run(
        worker._process_telegram_update(database, "test-token", 85, update, 1_793_000_000_000)
    )

    assert response == "📚 Course material saved."
    query, values = database.runs[1]
    assert "INSERT INTO course_resources" in query
    assert "ON CONFLICT(chat_id, source_message_id) DO UPDATE" in query
    assert values == (
        "college-1",
        "-100123456789",
        42,
        84,
        "https://t.me/c/123456789/84",
        "document",
        "lecture.pdf",
        None,
        "telegram-file-id",
        None,
        1_793_000_000_000,
    )


def test_weekly_digest_window_uses_sunday_6pm_addis(monkeypatch):
    worker = load_worker(monkeypatch)
    # 2026-11-01 is Sunday; these timestamps correspond to 17:55 and 18:00 Addis.
    before = 1_793_544_900_000
    start = 1_793_545_200_000
    last_minute = start + 59 * 60 * 1000
    next_hour = start + 60 * 60 * 1000

    assert worker.is_weekly_digest_window(before) is False
    assert worker.is_weekly_digest_window(start) is True
    assert worker.is_weekly_digest_window(last_minute) is True
    assert worker.is_weekly_digest_window(next_hour) is False


def test_weekly_digest_text_summarizes_upcoming_events_and_review_state(monkeypatch):
    worker = load_worker(monkeypatch)
    message = worker.format_weekly_digest(
        [
            {
                "item_type": "assignment",
                "title": "Operating Systems HW 2",
                "deadline_ms": 1_800_000_000_000,
                "item_status": "active",
            },
            {
                "item_type": "exam",
                "title": "Midterm",
                "deadline_ms": 1_800_100_000_000,
                "item_status": "new",
            },
        ]
    )

    assert "This week" in message
    assert "Operating Systems HW 2" in message
    assert "Midterm" in message
    assert "Needs class admin review" in message


def test_digest_subscription_requires_private_bot_start(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.statement_results = None
    update = {
        "message": {
            "message_id": 91,
            "chat": {"id": -100123456789, "type": "supergroup"},
            "from": {"id": 77},
            "text": "/digest_on",
        }
    }

    response = asyncio.run(
        worker._process_telegram_update(database, "test-token", 92, update, 1_793_000_000_000)
    )

    assert "start the bot privately" in response.lower()
    assert len(database.runs) == 1


def test_digest_subscription_is_opt_in_and_scoped_to_origin_topic(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.first_results = [{"chat_id": "-100123456789"}, {"user_id": 77}]
    update = {
        "message": {
            "message_id": 93,
            "message_thread_id": 42,
            "chat": {"id": -100123456789, "type": "supergroup"},
            "from": {"id": 77},
            "text": "/digest_on",
        }
    }

    response = asyncio.run(
        worker._process_telegram_update(database, "test-token", 94, update, 1_793_000_000_000)
    )

    assert "enabled" in response
    query, values = database.runs[-1]
    assert "ON CONFLICT(user_id, chat_id, scope_key) DO UPDATE" in query
    assert values == (77, "-100123456789", 42, "42", 1, 1_793_000_000_000, 1_793_000_000_000)


def test_digest_delivery_claim_is_bounded_and_lease_protected(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.claimed_reminders = [{"id": 11, "chat_id": "77", "message_text": "This week", "attempt_count": 1}]

    claimed = asyncio.run(worker.claim_due_weekly_digests(database, 50_000))

    assert claimed == database.claimed_reminders
    query, values = database.runs[0]
    assert "UPDATE weekly_digest_deliveries" in query
    assert "status = 'sending'" in query
    assert "lease_until_ms <= ?" in query
    assert values == (110_000, 5, 50_000, 50_000, 5, 50_000, 50_000)


def test_group_setup_command_is_private_and_validates_target_chat_and_code(monkeypatch):
    worker = load_worker(monkeypatch)

    assert worker.parse_group_setup_command("/group_setup -100123456789 college-join-code-123") == {
        "chat_id": "-100123456789",
        "invite_code": "college-join-code-123",
    }
    assert worker.parse_group_setup_command("/group_setup 12345 short") is None
    assert worker.parse_group_setup_command("/group_setup -100123456789") is None


def test_course_mapping_command_rejects_names_that_are_not_course_topics(monkeypatch):
    worker = load_worker(monkeypatch)

    assert worker.parse_course_map_command("/course_map Operating Systems") == "Operating Systems"
    assert worker.parse_course_map_command("/course_map") is None
    assert worker.parse_course_map_command("/course_map " + "x" * 300) is None


def test_course_name_normalization_is_case_and_whitespace_stable(monkeypatch):
    worker = load_worker(monkeypatch)

    assert worker.normalize_course_name("  Operating   Systems ") == "operating systems"


def test_group_id_command_returns_chat_id_for_group_onboarding(monkeypatch):
    worker = load_worker(monkeypatch)
    update = {
        "message": {
            "chat": {"id": -100123456789, "type": "supergroup"},
            "text": "/group_id",
        }
    }

    response = asyncio.run(
        worker._process_telegram_update(FakeDatabase(), "token", 1, update, 1_700_000_000_000)
    )

    assert "-100123456789" in response


def test_private_group_setup_consumes_code_without_echoing_it(monkeypatch):
    worker = load_worker(monkeypatch)
    received = {}

    async def fake_register(database, token, user_id, chat_id, invite_code, now_ms):
        received.update(user_id=user_id, chat_id=chat_id, invite_code=invite_code)
        return True

    monkeypatch.setattr(worker, "register_group_with_invite", fake_register)
    update = {
        "message": {
            "chat": {"id": 5, "type": "private"},
            "from": {"id": 5},
            "text": "/group_setup -100123456789 College-Code-1234",
        }
    }

    response = asyncio.run(
        worker._process_telegram_update(FakeDatabase(), "token", 2, update, 1_700_000_000_000)
    )

    assert received == {
        "user_id": 5,
        "chat_id": "-100123456789",
        "invite_code": "College-Code-1234",
    }
    assert "College-Code-1234" not in response
    assert "linked" in response


def test_guided_group_setup_previews_exact_and_uncertain_topic_mappings(monkeypatch):
    worker = load_worker(monkeypatch)
    state = {
        "status": "ready", "semester_label": "Fall 2026", "group_title": "CS Class",
        "run_id": 18,
        "decisions": [
            {"thread_id": 10, "topic_title": "Data Structures", "status": "auto_apply", "course_id": 4, "course_name": "Data Structures", "candidates": []},
            {"thread_id": 11, "topic_title": "Random Notes", "status": "needs_admin_choice", "candidates": [{"course_id": 5, "course_name": "Algorithms"}]},
        ],
        "unmapped_offerings": [{"course_name": "Algorithms"}],
    }
    monkeypatch.setattr(worker, "is_group_admin_or_representative", AsyncMock(return_value=True))
    monkeypatch.setattr(worker.group_setup_service, "begin_setup_run", AsyncMock(return_value=state))

    reply = asyncio.run(worker._guided_group_setup_command(
        FakeDatabase(), "token",
        {"from": {"id": 7}}, {"id": -1001, "type": "supergroup"}, "-1001", "/group_setup", 123,
    ))

    assert "Fall 2026" in reply.text
    buttons = reply.reply_markup["inline_keyboard"]
    assert any(button.get("callback_data") == "setup:apply:18" for row in buttons for button in row)
    assert any(button.get("callback_data") == "setup:choose:18:11:5" for row in buttons for button in row)
    assert "Algorithms" in reply.text


def test_setup_preview_explains_unseen_historical_topics(monkeypatch):
    worker = load_worker(monkeypatch)
    reply = worker._setup_preview_reply({
        "semester_label": "Fall 2026", "group_title": "Class", "run_id": 18,
        "decisions": [], "unmapped_offerings": [],
    })
    assert "/course_map Course Name" in reply.text
    assert "Send a message in each" not in reply.text


def test_guided_setup_prompts_for_semester_and_uses_force_reply(monkeypatch):
    worker = load_worker(monkeypatch)
    monkeypatch.setattr(worker, "is_group_admin_or_representative", AsyncMock(return_value=True))
    monkeypatch.setattr(worker.group_setup_service, "begin_setup_run", AsyncMock(return_value={
        "status": "semester_required",
    }))
    reply = asyncio.run(worker._guided_group_setup_command(
        FakeDatabase(), "token", {"from": {"id": 7}},
        {"id": -1001, "type": "supergroup"}, "-1001", "/group_setup", 123,
    ))
    assert reply.reply_markup["inline_keyboard"][0][0]["callback_data"] == "setup:semester"

    answer = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(worker, "answer_telegram_callback", answer)
    monkeypatch.setattr(worker, "send_telegram_message", send)
    monkeypatch.setattr(worker, "is_telegram_chat_admin", AsyncMock(return_value=True))
    update = {"callback_query": {
        "id": "cb", "from": {"id": 7}, "data": "setup:semester",
        "message": {"chat": {"id": -1001, "type": "supergroup"}, "message_thread_id": 20},
    }}
    asyncio.run(worker._handle_group_setup_callback_update(FakeDatabase(), "token", update, 123))
    sent = send.await_args.args[1]
    assert sent["thread_id"] == 20
    assert sent["reply_markup"]["force_reply"] is True


def test_semester_prompt_reply_uses_existing_authorized_setter(monkeypatch):
    worker = load_worker(monkeypatch)
    setter = AsyncMock(return_value="semester saved")
    monkeypatch.setattr(worker, "_semester_set_command", setter)
    database = FakeDatabase()
    message = {
        "chat": {"id": -1001, "type": "supergroup"},
        "from": {"id": 7}, "text": "Fall 2026",
        "reply_to_message": {"text": worker.HOME_SETUP_SEMESTER_PROMPT},
    }
    reply = asyncio.run(worker._process_message_update(
        database, "token", 5, message, 123, "", "model", "bot"
    ))
    assert reply == "semester saved"
    assert setter.await_args.args[5] == "/semester_set Fall 2026"


def test_forum_topic_created_is_recorded_for_guided_setup(monkeypatch):
    worker = load_worker(monkeypatch)
    observe = AsyncMock(return_value=True)
    monkeypatch.setattr(worker.group_setup_service, "observe_forum_topic", observe)
    database = FakeDatabase()
    update = {"message": {
        "message_id": 40, "chat": {"id": -1001, "type": "supergroup"},
        "from": {"id": 7}, "message_thread_id": 55,
        "forum_topic_created": {"name": "Operating Systems"},
    }}

    asyncio.run(worker._process_message_update(
        database, "token", 40, update["message"], 123, "", "model", "bot"
    ))

    observe.assert_awaited_once_with(database, "-1001", 55, "Operating Systems", 7, 123)


def test_forum_topic_without_thread_id_is_not_guessed(monkeypatch):
    worker = load_worker(monkeypatch)
    observe = AsyncMock(return_value=True)
    monkeypatch.setattr(worker.group_setup_service, "observe_forum_topic", observe)
    asyncio.run(worker._process_message_update(
        FakeDatabase(), "token", 40,
        {"chat": {"id": -1001, "type": "supergroup"},
         "forum_topic_created": {"name": "Operating Systems"}},
        123, "", "model", "bot",
    ))
    observe.assert_not_awaited()


def test_setup_callback_rechecks_admin_before_mapping(monkeypatch):
    worker = load_worker(monkeypatch)
    denied = AsyncMock(return_value=False)
    resolve = AsyncMock(return_value=True)
    answer = AsyncMock()
    monkeypatch.setattr(worker, "is_group_admin_or_representative", denied)
    monkeypatch.setattr(worker.group_setup_service, "resolve_topic_match", resolve)
    monkeypatch.setattr(worker, "answer_telegram_callback", answer)
    callback = {"id": "cb", "from": {"id": 8}, "data": "setup:choose:18:11:5",
                "message": {"chat": {"id": -1001, "type": "supergroup"}}}

    result = asyncio.run(worker._handle_group_setup_callback_update(FakeDatabase(), "token", {"callback_query": callback}, 100))

    assert result == "__QUIZ_HANDLED__"
    resolve.assert_not_awaited()
    answer.assert_awaited_once()


def test_setup_apply_callback_recomputes_exact_matches_and_maps_only_clear_topics(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.first_results = [{"id": 18}]
    monkeypatch.setattr(worker, "is_group_admin_or_representative", AsyncMock(return_value=True))
    monkeypatch.setattr(worker.group_setup_service, "get_group_setup_state", AsyncMock(return_value={
        "status": "ready", "topics": [{"thread_id": 10, "topic_title": "Data Structures", "current_course_id": None}],
        "offerings": [{"course_id": 4, "course_name": "Data Structures", "active": 1}],
    }))
    apply = AsyncMock(return_value={"status": "completed", "applied_count": 1})
    monkeypatch.setattr(worker.group_setup_service, "apply_exact_matches", apply)
    monkeypatch.setattr(worker, "answer_telegram_callback", AsyncMock())
    send = AsyncMock()
    monkeypatch.setattr(worker, "send_telegram_message", send)
    update = {"callback_query": {
        "id": "cb", "from": {"id": 8}, "data": "setup:apply:18",
        "message": {"chat": {"id": -1001, "type": "supergroup"}, "message_thread_id": 20},
    }}

    asyncio.run(worker._handle_group_setup_callback_update(database, "token", update, 100))

    args = apply.await_args.args
    assert args[:3] == (database, 18, 8)
    assert [(item["thread_id"], item["course_id"]) for item in args[3]] == [(10, 4)]
    send.assert_awaited_once()
    assert send.await_args.args[1]["thread_id"] == 20


def test_setup_callback_rejects_a_superseded_preview(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.first_results = [{"id": 19}]
    monkeypatch.setattr(worker, "is_group_admin_or_representative", AsyncMock(return_value=True))
    apply = AsyncMock()
    monkeypatch.setattr(worker.group_setup_service, "apply_exact_matches", apply)
    monkeypatch.setattr(worker, "answer_telegram_callback", AsyncMock())
    send = AsyncMock()
    monkeypatch.setattr(worker, "send_telegram_message", send)
    update = {"callback_query": {
        "id": "cb", "from": {"id": 8}, "data": "setup:apply:18",
        "message": {"chat": {"id": -1001, "type": "supergroup"}},
    }}
    asyncio.run(worker._handle_group_setup_callback_update(database, "token", update, 100))
    apply.assert_not_awaited()
    assert "expired" in send.await_args.args[1]["message_text"]


def test_unregistered_group_file_is_not_acknowledged_as_saved(monkeypatch):
    worker = load_worker(monkeypatch)
    database = FakeDatabase()
    database.statement_results = None
    update = {
        "message": {
            "message_id": 84,
            "chat": {"id": -100123456789, "type": "supergroup"},
            "document": {"file_id": "file", "file_name": "lecture.pdf"},
        }
    }

    response = asyncio.run(
        worker._process_telegram_update(database, "token", 3, update, 1_700_000_000_000)
    )

    assert "not connected to a college" in response


def test_course_map_command_maps_current_forum_topic(monkeypatch):
    worker = load_worker(monkeypatch)
    mapped = {}

    async def fake_map(database, token, chat_id, thread_id, course_name, user_id, now_ms):
        mapped.update(
            chat_id=chat_id,
            thread_id=thread_id,
            course_name=course_name,
            user_id=user_id,
        )
        return True

    monkeypatch.setattr(worker, "map_course_to_topic", fake_map)
    update = {
        "message": {
            "message_thread_id": 42,
            "chat": {"id": -100123456789, "type": "supergroup"},
            "from": {"id": 9},
            "text": "/course_map Operating Systems",
        }
    }

    response = asyncio.run(
        worker._process_telegram_update(FakeDatabase(), "token", 4, update, 1_700_000_000_000)
    )

    assert mapped == {
        "chat_id": "-100123456789",
        "thread_id": 42,
        "course_name": "Operating Systems",
        "user_id": 9,
    }
    assert "Operating Systems" in response


def test_group_admin_can_assign_representative_by_reply(monkeypatch):
    worker = load_worker(monkeypatch)

    async def fake_is_admin(*args):
        return True

    monkeypatch.setattr(worker, "is_telegram_chat_admin", fake_is_admin)

    async def fake_add(database, chat_id, user_id, added_by_user_id, now_ms):
        return (chat_id, user_id, added_by_user_id) == ("-100123456789", 10, 9)

    monkeypatch.setattr(worker, "add_group_representative", fake_add)
    update = {
        "message": {
            "chat": {"id": -100123456789, "type": "supergroup"},
            "from": {"id": 9},
            "reply_to_message": {"from": {"id": 10}},
            "text": "/rep_add",
        }
    }

    response = asyncio.run(
        worker._process_telegram_update(FakeDatabase(), "token", 5, update, 1_700_000_000_000)
    )

    assert "representative added" in response
    assert worker.parse_ai_academic_extraction(
        {
            "type": "quiz",
            "title": "Quiz",
            "deadline": "next Friday maybe",
            "confidence": 0.95,
        }
    ) is None


def test_private_quiz_start_checks_membership_and_sends_inline_options(monkeypatch):
    worker = load_worker(monkeypatch)
    sent = []

    async def fake_get_quiz(database, quiz_id):
        assert quiz_id == 7
        return {"id": 7, "chat_id": "-100123456789", "title": "Midterm"}

    async def fake_member(token, chat_id, user_id):
        assert (chat_id, user_id) == ("-100123456789", 77)
        return True

    async def fake_start(database, quiz_id, user_id, now_ms):
        return 9

    async def fake_next(database, attempt_id):
        return {"id": 12, "position": 1, "prompt": "Which?", "options": ["A", "B", "C", "D"]}

    async def fake_send(token, payload):
        sent.append(payload)

    monkeypatch.setattr(worker.quiz_service, "get_published_quiz", fake_get_quiz)
    monkeypatch.setattr(worker, "is_telegram_chat_member", fake_member)
    monkeypatch.setattr(worker.quiz_service, "start_quiz_attempt", fake_start)
    monkeypatch.setattr(worker.quiz_service, "_next_question", fake_next)
    monkeypatch.setattr(worker, "send_telegram_message", fake_send)
    update = {"message": {
        "chat": {"id": 77, "type": "private"}, "from": {"id": 77},
        "text": "/start quiz_7",
    }}

    result = asyncio.run(worker._process_telegram_update(FakeDatabase(), "token", 1, update, 100))

    assert result == "__QUIZ_HANDLED__"
    assert sent[0]["chat_id"] == "77"
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "quiz:9:12:0"


def test_private_quiz_start_rejects_nonmembers_without_revealing_questions(monkeypatch):
    worker = load_worker(monkeypatch)
    sent = []

    async def fake_get_quiz(database, quiz_id):
        return {"id": quiz_id, "chat_id": "-100123456789", "title": "Midterm"}

    async def fake_member(token, chat_id, user_id):
        return False

    async def fake_send(token, payload):
        sent.append(payload)

    monkeypatch.setattr(worker.quiz_service, "get_published_quiz", fake_get_quiz)
    monkeypatch.setattr(worker, "is_telegram_chat_member", fake_member)
    monkeypatch.setattr(worker, "send_telegram_message", fake_send)
    update = {"message": {
        "chat": {"id": 77, "type": "private"}, "from": {"id": 77},
        "text": "/start quiz_7",
    }}

    result = asyncio.run(worker._process_telegram_update(FakeDatabase(), "token", 1, update, 100))

    assert "Join the class group" in result
    assert sent == []


def test_quiz_callback_sends_completed_score_only_to_private_chat(monkeypatch):
    worker = load_worker(monkeypatch)
    sent = []
    answered = []

    async def fake_submit(database, attempt_id, question_id, option, user_id, now_ms):
        assert (attempt_id, question_id, option, user_id) == (9, 12, 1, 77)
        return {"status": "completed", "score": 1, "total_questions": 2}

    async def fake_answer(token, callback_id, text=""):
        answered.append((callback_id, text))

    async def fake_send(token, payload):
        sent.append(payload)

    monkeypatch.setattr(worker.quiz_service, "submit_quiz_answer", fake_submit)
    monkeypatch.setattr(worker, "answer_telegram_callback", fake_answer)
    monkeypatch.setattr(worker, "send_telegram_message", fake_send)
    update = {"callback_query": {
        "id": "callback-1", "from": {"id": 77}, "data": "quiz:9:12:1",
        "message": {"chat": {"id": 77, "type": "private"}},
    }}

    result = asyncio.run(worker._process_telegram_update(FakeDatabase(), "token", 2, update, 200))

    assert result == "__QUIZ_HANDLED__"
    assert answered == [("callback-1", "")]
    assert sent == [{
        "chat_id": "77",
        "message_text": "Quiz complete. You scored <b>1/2</b>. Your result is private.",
    }]


def test_registered_rep_posts_anonymous_poll_in_current_topic(monkeypatch):
    worker = load_worker(monkeypatch)
    posted = []

    async def fake_authorized(database, token, chat_id, user_id):
        return (chat_id, user_id) == ("-100123456789", 9)

    async def fake_send_once(database, token, update_id, poll, now_ms):
        posted.append((update_id, poll))
        return True

    monkeypatch.setattr(worker, "is_group_admin_or_representative", fake_authorized)
    monkeypatch.setattr(worker, "send_class_poll_once", fake_send_once)
    update = {"message": {
        "chat": {"id": -100123456789, "type": "supergroup"},
        "message_thread_id": 42,
        "from": {"id": 9},
        "text": "/class_poll Which review? | Morning | Afternoon",
    }}

    response = asyncio.run(
        worker._process_telegram_update(FakeDatabase(), "token", 123, update, 456)
    )

    assert "Anonymous poll posted" in response
    assert posted == [(123, {
        "question": "Which review?",
        "options": ["Morning", "Afternoon"],
        "chat_id": "-100123456789",
        "thread_id": 42,
    })]


def test_unregistered_user_cannot_post_class_poll(monkeypatch):
    worker = load_worker(monkeypatch)
    calls = []

    async def fake_authorized(*args):
        return False

    async def fake_send_once(*args):
        calls.append(args)
        return True

    monkeypatch.setattr(worker, "is_group_admin_or_representative", fake_authorized)
    monkeypatch.setattr(worker, "send_class_poll_once", fake_send_once)
    update = {"message": {
        "chat": {"id": -100123456789, "type": "supergroup"},
        "from": {"id": 99},
        "text": "/class_poll Pick a day | Monday | Tuesday",
    }}

    response = asyncio.run(
        worker._process_telegram_update(FakeDatabase(), "token", 124, update, 457)
    )

    assert "Only registered class reps" in response
    assert calls == []


def test_telegram_poll_transport_posts_anonymous_single_choice_thread_poll(monkeypatch):
    worker = load_worker(monkeypatch)
    captured = []

    class FakeResponse:
        status = 200

        async def json(self):
            return {"ok": True, "result": {"poll": {"id": "poll-1"}}}

    async def fake_fetch(url, **kwargs):
        captured.append((url, json.loads(kwargs["body"])))
        return FakeResponse()

    monkeypatch.setattr(sys.modules["workers"], "fetch", fake_fetch, raising=False)
    poll_id = asyncio.run(worker.send_telegram_poll("token", {
        "chat_id": "-100123456789",
        "thread_id": 42,
        "question": "Which day?",
        "options": [{"text": "Monday"}, {"text": "Tuesday"}],
    }))

    assert poll_id == "poll-1"
    assert captured[0][0].endswith("/sendPoll")
    assert captured[0][1] == {
        "chat_id": "-100123456789",
        "question": "Which day?",
        "options": [{"text": "Monday"}, {"text": "Tuesday"}],
        "is_anonymous": True,
        "type": "regular",
        "allows_multiple_answers": False,
        "message_thread_id": 42,
    }


def test_telegram_quiz_transport_sets_correct_answer_and_quiz_type(monkeypatch):
    worker = load_worker(monkeypatch)
    captured = []

    class FakeResponse:
        status = 200

        async def json(self):
            return {"ok": True, "result": {"poll": {"id": "quiz-poll-1"}}}

    async def fake_fetch(url, **kwargs):
        captured.append(json.loads(kwargs["body"]))
        return FakeResponse()

    monkeypatch.setattr(sys.modules["workers"], "fetch", fake_fetch, raising=False)
    poll_id = asyncio.run(worker.send_telegram_poll("token", {
        "chat_id": "-100123456789",
        "thread_id": 88,
        "question": "Which is correct?",
        "options": ["A", "B", "C", "D"],
        "correct_option_id": 2,
        "type": "quiz",
        "is_anonymous": True,
    }))

    assert poll_id == "quiz-poll-1"
    assert captured[0]["type"] == "quiz"
    assert captured[0]["correct_option_ids"] == [2]
    assert captured[0]["options"] == [
        {"text": "A"}, {"text": "B"}, {"text": "C"}, {"text": "D"}
    ]
    assert captured[0]["message_thread_id"] == 88


def test_duplicate_poll_update_is_not_sent_twice(monkeypatch):
    worker = load_worker(monkeypatch)
    sends = []

    async def already_claimed(database, update_id, chat_id, now_ms):
        return False

    async def fake_send(token, poll):
        sends.append(poll)
        return "poll-duplicate"

    async def fake_finish(*args, **kwargs):
        return None

    monkeypatch.setattr(worker.poll_service, "claim_class_poll", already_claimed)
    monkeypatch.setattr(worker.poll_service, "finish_class_poll", fake_finish)
    monkeypatch.setattr(worker, "send_telegram_poll", fake_send)

    sent = asyncio.run(worker.send_class_poll_once(
        FakeDatabase(), "token", 123, {"chat_id": "-100123456789", "question": "Q", "options": ["A", "B"]}, 456
    ))

    assert sent is False
    assert sends == []
