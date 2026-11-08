import importlib.util
import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType


def load_quiz_service():
    service_path = Path(__file__).parents[1] / "src" / "quiz_service.py"
    spec = importlib.util.spec_from_file_location("cloudflare_quiz_service_under_test", service_path)
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    return service


def load_course_quiz_service():
    service_path = Path(__file__).parents[1] / "src" / "course_quiz_service.py"
    spec = importlib.util.spec_from_file_location("cloudflare_course_quiz_service_under_test", service_path)
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    return service


def test_telegram_file_download_reads_bytes_from_workers_response(monkeypatch):
    service = load_course_quiz_service()
    calls = []

    payload = b"pdf"

    class Response:
        status = 200

        def __init__(self, body=None):
            self.body = body

        async def json(self):
            return self.body

        async def bytes(self):
            return payload

    responses = [
        Response({"ok": True, "result": {"file_path": "documents/sample.pdf", "file_size": 3}}),
        Response(),
    ]

    async def fetch(url, **options):
        calls.append(url)
        return responses.pop(0)

    workers = ModuleType("workers")
    workers.fetch = fetch
    monkeypatch.setitem(sys.modules, "workers", workers)

    data, mime = asyncio.run(service._download_telegram_file(
        "test-token", {"file_id": "sample-id", "file_size": 3, "title": "sample.pdf"}
    ))

    assert data is payload
    assert mime == "application/pdf"
    assert len(calls) == 2


def test_markdown_conversion_uses_workers_python_blob_wrapper(monkeypatch):
    service = load_course_quiz_service()
    blob_value = object()

    class Blob:
        def __init__(self, parts, content_type=None):
            assert parts == b"sample-image"
            assert content_type == "image/jpeg"
            self.js_object = blob_value

    workers = ModuleType("workers")
    workers.Blob = Blob
    monkeypatch.setitem(sys.modules, "workers", workers)
    js_module = ModuleType("js")
    js_module.Object = SimpleNamespace(fromEntries=object())
    monkeypatch.setitem(sys.modules, "js", js_module)
    pyodide = ModuleType("pyodide")
    ffi = ModuleType("pyodide.ffi")
    ffi.to_js = lambda value, **_kwargs: value
    pyodide.ffi = ffi
    monkeypatch.setitem(sys.modules, "pyodide", pyodide)
    monkeypatch.setitem(sys.modules, "pyodide.ffi", ffi)

    class AI:
        async def toMarkdown(self, documents):
            assert documents[0]["blob"] is blob_value
            return [{"format": "markdown", "data": "Image description"}]

    result = asyncio.run(service._convert_file_to_markdown(
        AI(), {"title": "sample.jpg"}, b"sample-image", "image/jpeg"
    ))

    assert result == "Image description"


class SqliteStatement:
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
        self.database.connection.commit()
        return dict(row) if row else None

    async def all(self):
        rows = self.database.connection.execute(self.query, self.values).fetchall()
        return {"results": [dict(row) for row in rows]}

    async def run(self):
        cursor = self.database.connection.execute(self.query, self.values)
        self.database.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class SqliteDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        migration_dir = Path(__file__).parents[1] / "migrations"
        for migration in sorted(migration_dir.glob("*.sql")):
            self.connection.executescript(migration.read_text(encoding="utf-8"))

    def prepare(self, query):
        return SqliteStatement(self, query)

    def seed_mapped_topic(self, chat_id="-100123456789", thread_id=42):
        self.connection.execute(
            "INSERT INTO colleges (college_id, college_name, created_at_ms) VALUES ('college-1', 'College One', 1)"
        )
        self.connection.execute(
            "INSERT INTO academic_groups (chat_id, college_id, group_title, created_by_user_id, created_at_ms) VALUES (?, 'college-1', 'Class', 1, 1)",
            (chat_id,),
        )
        self.connection.execute(
            "INSERT INTO courses (college_id, chat_id, course_name, normalized_name, created_at_ms) VALUES ('college-1', ?, 'Databases', 'databases', 1)",
            (chat_id,),
        )
        course_id = self.connection.execute("SELECT id FROM courses").fetchone()[0]
        self.connection.execute(
            "INSERT INTO group_topics (chat_id, thread_id, course_id, created_by_user_id, created_at_ms) VALUES (?, ?, ?, 1, 1)",
            (chat_id, thread_id, course_id),
        )
        self.connection.commit()


def test_quiz_create_parser_requires_a_bounded_title():
    service = load_quiz_service()

    assert service.parse_quiz_create_command("/quiz_create Midterm Practice") == "Midterm Practice"
    assert service.parse_quiz_create_command("/quiz_create") is None
    assert service.parse_quiz_create_command("/quiz_create " + "x" * 201) is None


def test_quiz_question_parser_requires_four_options_and_a_valid_one_based_answer():
    service = load_quiz_service()

    parsed = service.parse_quiz_question_command(
        "/quiz_question 12 | Which is a database? | PostgreSQL | HTTP | HTML | CSS | 1 | Stores structured records"
    )
    assert parsed == {
        "quiz_id": 12,
        "prompt": "Which is a database?",
        "options": ["PostgreSQL", "HTTP", "HTML", "CSS"],
        "correct_option_index": 0,
        "explanation": "Stores structured records",
    }
    assert service.parse_quiz_question_command("/quiz_question 12 | Q | A | B | 1") is None
    assert service.parse_quiz_question_command("/quiz_question 12 | Q | A | B | C | D | 5") is None


def test_quiz_start_payload_and_id_commands_are_strict():
    service = load_quiz_service()

    assert service.parse_quiz_start_payload("quiz_123") == 123
    assert service.parse_quiz_start_payload("quiz_0") is None
    assert service.parse_quiz_start_payload("other_123") is None
    assert service.parse_quiz_id_command("/quiz_publish 123", "quiz_publish") == 123
    assert service.parse_quiz_id_command("/quiz_results 123", "quiz_results") == 123
    assert service.parse_quiz_id_command("/quiz_results 1 2", "quiz_results") is None


def test_semester_and_topic_commands_are_bounded():
    service = load_course_quiz_service()

    assert service.parse_semester_set_command("/semester_set Fall 2026") == "Fall 2026"
    assert service.parse_semester_set_command("/semester_set   2026-Spring  ") == "2026-Spring"
    assert service.parse_semester_set_command("/semester_set") is None
    assert service.parse_semester_set_command("/semester_set " + "x" * 41) is None
    assert service.parse_quiz_source_command("/quiz_source") is True
    assert service.parse_quiz_source_command("/quiz_source@academicgroupmanagementbot") is True
    assert service.parse_quiz_source_command("/quiz_source extra") is False


def test_quiz_tick_processes_one_tutor_index_job_before_other_ai_work():
    import asyncio

    service = load_course_quiz_service()
    calls = []

    async def process_index(database, now_ms):
        calls.append((database, now_ms))
        return True

    database = object()
    result = asyncio.run(service.process_quiz_tick(
        database, "token", object(), 1234, None, None, process_index=process_index,
    ))

    assert result == "tutor_source_indexed"
    assert calls == [(database, 1234)]


def test_addis_course_quiz_schedule_is_every_other_day_at_ten():
    from datetime import datetime, timezone

    service = load_course_quiz_service()
    # 2026-11-04 is an even ordinal day; 10:00 Addis is 07:00 UTC.
    due = int(datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc).timestamp() * 1000)
    next_day = due + 24 * 60 * 60 * 1000
    wrong_hour = due + 60 * 60 * 1000

    assert service.is_course_quiz_window(due)
    assert not service.is_course_quiz_window(next_day)
    assert not service.is_course_quiz_window(wrong_hour)


def test_exam_prep_selects_only_exams_due_next_addis_day():
    from datetime import datetime, timezone

    service = load_course_quiz_service()
    now_ms = int(datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)
    tomorrow_deadline = int(datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc).timestamp() * 1000)
    day_after_deadline = int(datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc).timestamp() * 1000)

    assert service.exam_is_tomorrow(now_ms, tomorrow_deadline)
    assert not service.exam_is_tomorrow(now_ms, day_after_deadline)


def test_generated_questions_require_four_distinct_options_and_grounded_source_ids():
    service = load_course_quiz_service()
    question = {
        "prompt": "Which data structure stores values by key?",
        "options": ["Map", "Queue", "Stack", "Tree"],
        "correct_option_index": 0,
        "source_message_id": 55,
        "source_excerpt": "A map stores values using unique keys.",
    }

    assert service.validate_generated_questions({"questions": [question]}, 1, {55}) == [question]
    assert service.validate_generated_questions(
        {"questions": [question]}, 1, {55}, {55: "A map stores values using unique keys."}
    ) == [question]
    assert service.validate_generated_questions(
        {"questions": [question]}, 1, {55}, {55: "A queue stores values in FIFO order."}
    ) is None
    assert service.validate_generated_questions({"questions": [question]}, 1, {44}) is None
    assert service.validate_generated_questions(
        {"questions": [{**question, "options": ["Map", "map", "Stack", "Tree"]}]}, 1, {55}
    ) is None


def test_marked_text_source_is_saved_only_from_the_mapped_current_term_topic():
    import asyncio

    service = load_course_quiz_service()
    database = SqliteDatabase()
    database.seed_mapped_topic()
    assert asyncio.run(service.set_group_semester(database, "-100123456789", "Fall 2026", 1))
    note = {
        "message_id": 101,
        "message_thread_id": 42,
        "text": "A relational database organizes information into tables with rows and columns.",
    }

    saved = asyncio.run(service.save_quiz_text_source(database, "-100123456789", 42, note, 9, 100))
    wrong_topic = asyncio.run(service.save_quiz_text_source(database, "-100123456789", 77, note, 9, 101))
    row = database.connection.execute(
        "SELECT course_id, semester_label, extraction_state, extracted_text FROM course_quiz_sources"
    ).fetchone()

    assert saved is True
    assert wrong_topic is False
    assert tuple(row) == (1, "Fall 2026", "ready", note["text"])
    offering = database.connection.execute(
        "SELECT semester_label, active FROM course_offerings WHERE chat_id = '-100123456789'"
    ).fetchone()
    assert tuple(offering) == ("Fall 2026", 1)


def test_regular_quiz_run_is_queued_once_for_an_eligible_current_term_course():
    import asyncio
    from datetime import datetime, timezone

    service = load_course_quiz_service()
    database = SqliteDatabase()
    database.seed_mapped_topic()
    database.connection.execute("UPDATE academic_groups SET current_semester = 'Fall 2026'")
    database.connection.execute(
        "INSERT INTO course_offerings (college_id, chat_id, course_id, semester_label, created_at_ms) VALUES ('college-1', '-100123456789', 1, 'Fall 2026', 1)"
    )
    database.connection.execute(
        "INSERT INTO quiz_topic_settings (chat_id, college_id, thread_id, set_by_user_id, updated_at_ms) VALUES ('-100123456789', 'college-1', 88, 9, 1)"
    )
    database.connection.execute(
        "INSERT INTO course_quiz_sources (college_id, chat_id, course_id, semester_label, thread_id, source_message_id, resource_type, title, extracted_text, extraction_state, created_at_ms, updated_at_ms) VALUES ('college-1', '-100123456789', 1, 'Fall 2026', 42, 55, 'note', 'Notes', ?, 'ready', 1, 1)",
        ("Database design notes. " * 100,),
    )
    database.connection.commit()
    due = int(datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc).timestamp() * 1000)

    queued = asyncio.run(service.queue_due_runs(database, due))
    duplicate = asyncio.run(service.queue_due_runs(database, due + 5 * 60_000))
    run = database.connection.execute(
        "SELECT run_kind, run_key, course_id, semester_label, target_thread_id FROM quiz_generation_runs"
    ).fetchone()

    assert queued == 1
    assert duplicate == 0
    assert tuple(run) == ("regular", "regular:2026-11-04", 1, "Fall 2026", 88)


def test_exam_review_run_uses_only_tomorrows_current_term_exam():
    import asyncio
    from datetime import datetime, timezone

    service = load_course_quiz_service()
    database = SqliteDatabase()
    database.seed_mapped_topic()
    database.connection.execute("UPDATE academic_groups SET current_semester = 'Fall 2026'")
    database.connection.execute(
        "INSERT INTO course_offerings (college_id, chat_id, course_id, semester_label, created_at_ms) VALUES ('college-1', '-100123456789', 1, 'Fall 2026', 1)"
    )
    database.connection.execute(
        "INSERT INTO quiz_topic_settings (chat_id, college_id, thread_id, set_by_user_id, updated_at_ms) VALUES ('-100123456789', 'college-1', 88, 9, 1)"
    )
    database.connection.execute(
        "INSERT INTO course_quiz_sources (college_id, chat_id, course_id, semester_label, thread_id, source_message_id, resource_type, title, extracted_text, extraction_state, created_at_ms, updated_at_ms) VALUES ('college-1', '-100123456789', 1, 'Fall 2026', 42, 55, 'note', 'Notes', ?, 'ready', 1, 1)",
        ("Database design notes. " * 100,),
    )
    deadline = int(datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc).timestamp() * 1000)
    database.connection.execute(
        "INSERT INTO academic_items (source_item_id, group_id, chat_id, item_type, title, deadline_ms, item_status, updated_at_ms, college_id, course_id) VALUES (501, -100123456789, '-100123456789', 'exam', 'Midterm', ?, 'active', 1, 'college-1', 1)",
        (deadline,),
    )
    database.connection.commit()
    due = int(datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)

    queued = asyncio.run(service.queue_due_runs(database, due))
    assert queued == 2
    exam_run = database.connection.execute(
        "SELECT run_kind, run_key, exam_item_id FROM quiz_generation_runs WHERE run_kind = 'exam'"
    ).fetchone()
    assert tuple(exam_run) == ("exam", "exam:2026-11-05:501", 501)


def test_generated_quiz_is_persisted_before_non_replayable_poll_delivery():
    import asyncio
    import json

    service = load_course_quiz_service()
    database = SqliteDatabase()
    database.seed_mapped_topic()
    database.connection.execute("UPDATE academic_groups SET current_semester = 'Fall 2026'")
    database.connection.execute(
        "INSERT INTO course_offerings (college_id, chat_id, course_id, semester_label, created_at_ms) VALUES ('college-1', '-100123456789', 1, 'Fall 2026', 1)"
    )
    database.connection.execute(
        "INSERT INTO quiz_topic_settings (chat_id, college_id, thread_id, set_by_user_id, updated_at_ms) VALUES ('-100123456789', 'college-1', 88, 9, 1)"
    )
    database.connection.execute(
        "INSERT INTO course_quiz_sources (college_id, chat_id, course_id, semester_label, thread_id, source_message_id, resource_type, title, extracted_text, extraction_state, created_at_ms, updated_at_ms) VALUES ('college-1', '-100123456789', 1, 'Fall 2026', 42, 55, 'note', 'Notes', ?, 'ready', 1, 1)",
        ("Database design notes. " * 100,),
    )
    database.connection.execute(
        "INSERT INTO quiz_generation_runs (college_id, chat_id, semester_label, course_id, target_thread_id, run_kind, run_key, status, attempt_count, created_at_ms, updated_at_ms) VALUES ('college-1', '-100123456789', 'Fall 2026', 1, 88, 'regular', 'regular:2026-11-04', 'generating', 1, 1, 1)"
    )
    database.connection.commit()
    questions = [{
        "prompt": f"Which concept is described in note {index}?",
        "options": [f"Map {index}", f"Queue {index}", f"Stack {index}", f"Tree {index}"],
        "correct_option_index": 0,
        "source_message_id": 55,
        "source_excerpt": "Database design notes.",
    } for index in range(5)]

    class FakeAI:
        async def run(self, model, payload):
            assert model == "@cf/zai-org/glm-4.7-flash"
            assert payload["max_completion_tokens"] == 5000
            return {"choices": [{"message": {"content": json.dumps({"questions": questions})}}]}

    run = database.connection.execute("SELECT * FROM quiz_generation_runs").fetchone()
    saved = asyncio.run(service.generate_run_questions(database, FakeAI(), dict(run), 100))
    header = asyncio.run(service.claim_poll_delivery(database, 101))
    asyncio.run(service.finish_poll_delivery(database, header["id"], 102, uncertain=True))
    next_delivery = asyncio.run(service.claim_poll_delivery(database, 103))
    delivery_row = database.connection.execute(
        "SELECT status FROM quiz_poll_deliveries WHERE id = ?", (header["id"],)
    ).fetchone()
    payload = json.loads(next_delivery["payload_json"])

    assert saved is True
    assert database.connection.execute("SELECT COUNT(*) FROM generated_quiz_questions").fetchone()[0] == 5
    assert database.connection.execute("SELECT COUNT(*) FROM quiz_poll_deliveries").fetchone()[0] == 6
    assert header["delivery_kind"] == "header"
    assert delivery_row["status"] == "uncertain"
    assert next_delivery["delivery_kind"] == "poll"
    assert payload["chat_id"] == "-100123456789"
    assert payload["thread_id"] == 88
    assert payload["type"] == "quiz"


def test_ai_usage_has_a_durable_daily_call_budget():
    import asyncio

    service = load_course_quiz_service()
    database = SqliteDatabase()
    now_ms = 1_793_000_000_000

    for _ in range(service.DAILY_EXTRACTION_CALL_LIMIT):
        assert asyncio.run(service.consume_daily_ai_budget(database, now_ms, "extraction"))
    assert not asyncio.run(service.consume_daily_ai_budget(database, now_ms, "extraction"))
    for _ in range(service.DAILY_GENERATION_CALL_LIMIT):
        assert asyncio.run(service.consume_daily_ai_budget(database, now_ms, "generation"))
    assert not asyncio.run(service.consume_daily_ai_budget(database, now_ms, "generation"))
    usage = asyncio.run(service.daily_ai_usage(database, now_ms))

    assert usage == {
        "extraction_calls": service.DAILY_EXTRACTION_CALL_LIMIT,
        "generation_calls": service.DAILY_GENERATION_CALL_LIMIT,
    }


def test_pdf_ingestion_queues_mapped_current_semester_files_and_marks_oversized_files():
    import asyncio

    service = load_course_quiz_service()
    database = SqliteDatabase()
    database.seed_mapped_topic()
    assert asyncio.run(service.set_group_semester(database, "-100123456789", "Fall 2026", 1))
    resource = {
        "thread_id": 42,
        "source_message_id": 61,
        "message_link": "https://t.me/c/123456789/61",
        "resource_type": "document",
        "title": "week-1.pdf",
        "caption": "Week one lecture notes",
        "file_id": "telegram-file-id",
        "mime_type": "application/pdf",
        "file_size": 1024,
    }

    queued = asyncio.run(service.save_quiz_file_source(database, "-100123456789", resource, 10))
    duplicate = asyncio.run(service.save_quiz_file_source(database, "-100123456789", resource, 11))
    too_large = asyncio.run(service.save_quiz_file_source(
        database, "-100123456789", {**resource, "source_message_id": 62, "file_size": service.MAX_FILE_BYTES + 1}, 12
    ))
    rows = database.connection.execute(
        "SELECT source_message_id, extraction_state, last_error, caption FROM course_quiz_sources ORDER BY source_message_id"
    ).fetchall()

    assert queued is True
    assert duplicate is False
    assert too_large is True
    assert [tuple(row) for row in rows] == [
        (61, "pending", None, "Week one lecture notes"),
        (62, "ignored", "file_too_large", "Week one lecture notes"),
    ]


def test_quiz_storage_requires_mapped_active_group_and_matching_chat():
    import asyncio

    service = load_quiz_service()
    database = SqliteDatabase()
    database.seed_mapped_topic()

    quiz_id = asyncio.run(service.create_quiz(database, "-100123456789", 42, 9, "Midterm Practice", 10))
    wrong_topic = asyncio.run(service.create_quiz(database, "-100123456789", 77, 9, "Other", 10))
    wrong_chat_question = asyncio.run(
        service.add_quiz_question(database, "-100999999999", quiz_id, {
            "prompt": "A question?", "options": ["A", "B", "C", "D"],
            "correct_option_index": 0, "explanation": None,
        }, 11)
    )

    assert quiz_id == 1
    assert wrong_topic is None
    assert wrong_chat_question is None
    assert asyncio.run(service.publish_quiz(database, "-100123456789", quiz_id, 12)) is None


def test_private_attempt_is_idempotent_and_group_results_are_aggregate_only():
    import asyncio

    service = load_quiz_service()
    database = SqliteDatabase()
    database.seed_mapped_topic()
    quiz_id = asyncio.run(service.create_quiz(database, "-100123456789", 42, 9, "Midterm Practice", 10))
    questions = [
        {
            "prompt": f"Question {index}?",
            "options": ["A", "B", "C", "D"],
            "correct_option_index": index - 1,
            "explanation": None,
        }
        for index in (1, 2)
    ]
    for question in questions:
        assert asyncio.run(service.add_quiz_question(database, "-100123456789", quiz_id, question, 11))
    assert asyncio.run(service.publish_quiz(database, "-100123456789", quiz_id, 12))

    attempt_id = asyncio.run(service.start_quiz_attempt(database, quiz_id, 77, 13))
    first_question = asyncio.run(service._next_question(database, attempt_id))
    first_answer = asyncio.run(
        service.submit_quiz_answer(database, attempt_id, first_question["id"], 0, 77, 14)
    )
    duplicate_answer = asyncio.run(
        service.submit_quiz_answer(database, attempt_id, first_question["id"], 3, 77, 15)
    )
    second_question = first_answer["question"]
    final = asyncio.run(
        service.submit_quiz_answer(database, attempt_id, second_question["id"], 1, 77, 16)
    )
    summary = asyncio.run(service.quiz_results_summary(database, "-100123456789", quiz_id))
    leaked_summary = asyncio.run(service.quiz_results_summary(database, "-100999999999", quiz_id))

    assert first_answer["status"] == "in_progress"
    assert duplicate_answer["duplicate"] is True
    assert duplicate_answer["question"]["id"] == second_question["id"]
    assert final == {"status": "completed", "score": 2, "total_questions": 2}
    assert summary["attempt_count"] == 1
    assert summary["average_percent"] == 100
    assert "user_id" not in summary
    assert leaked_summary is None
