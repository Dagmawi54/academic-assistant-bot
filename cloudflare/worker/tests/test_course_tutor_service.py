import importlib.util
import sqlite3
import sys
from pathlib import Path


def load_service():
    source_dir = Path(__file__).parents[1] / "src"
    sys.path.insert(0, str(source_dir))
    service_path = source_dir / "course_tutor_service.py"
    spec = importlib.util.spec_from_file_location("course_tutor_service_under_test", service_path)
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    return service


class SqliteStatement:
    def __init__(self, database, query):
        self.database = database
        self.query = query
        self.values = ()

    def bind(self, *values):
        self.values = values
        return self

    async def first(self):
        row = self.database.connection.execute(self.query, self.values).fetchone()
        self.database.connection.commit()
        return dict(row) if row else None

    async def all(self):
        rows = self.database.connection.execute(self.query, self.values).fetchall()
        self.database.connection.commit()
        return {"results": [dict(row) for row in rows]}

    async def run(self):
        self.database.connection.execute(self.query, self.values)
        self.database.connection.commit()
        return {"success": True}


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

    def seed_course(self):
        self.connection.execute(
            "INSERT INTO colleges (college_id, college_name, created_at_ms) VALUES ('college-a', 'College A', 1)"
        )
        self.connection.execute(
            "INSERT INTO academic_groups (chat_id, college_id, group_title, created_by_user_id, created_at_ms, current_semester) VALUES ('-1001', 'college-a', 'Group A', 1, 1, 'Fall 2026')"
        )
        self.connection.execute(
            "INSERT INTO courses (college_id, chat_id, course_name, normalized_name, created_at_ms) VALUES ('college-a', '-1001', 'Databases', 'databases', 1)"
        )
        course_id = self.connection.execute("SELECT id FROM courses").fetchone()[0]
        self.connection.execute(
            "INSERT INTO group_topics (chat_id, thread_id, course_id, created_by_user_id, created_at_ms) VALUES ('-1001', 42, ?, 1, 1)",
            (course_id,),
        )
        self.connection.execute(
            "INSERT INTO course_offerings (college_id, chat_id, course_id, semester_label, created_at_ms) VALUES ('college-a', '-1001', ?, 'Fall 2026', 1)",
            (course_id,),
        )
        self.connection.execute(
            "INSERT INTO course_quiz_sources (college_id, chat_id, course_id, semester_label, thread_id, source_message_id, message_link, resource_type, title, extracted_text, extraction_state, created_at_ms, updated_at_ms) VALUES ('college-a', '-1001', ?, 'Fall 2026', 42, 900, 'https://t.me/c/1001/900', 'note', 'Relational algebra', ?, 'ready', 1, 1)",
            (course_id, "Relational algebra uses selection and projection to derive a relation from a database table. Normalization reduces duplication. " * 45),
        )
        self.connection.commit()
        return course_id


def test_tutor_memory_migration_creates_user_profiles_and_context_messages():
    database = SqliteDatabase()
    tables = {
        row[0]
        for row in database.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }

    assert {"tutor_user_profiles", "tutor_conversation_messages"} <= tables
    profile_columns = {
        row[1] for row in database.connection.execute(
            "PRAGMA table_info(tutor_user_profiles)"
        )
    }
    message_columns = {
        row[1] for row in database.connection.execute(
            "PRAGMA table_info(tutor_conversation_messages)"
        )
    }
    assert {"user_id", "study_preferences", "updated_at_ms"} <= profile_columns
    assert {"id", "user_id", "chat_id", "context_key", "role", "content", "created_at_ms"} <= message_columns


def test_tutor_memory_storage_is_scoped_bounded_and_deletable():
    import asyncio

    service = load_service()
    database = SqliteDatabase()

    async def exercise():
        await service.set_tutor_preferences(database, 41, "  Use examples  ", 100)
        assert await service.get_tutor_preferences(database, 41) == "Use examples"
        assert await service.get_tutor_preferences(database, 42) == ""

        for index in range(1, 10):
            await service.save_tutor_exchange(
                database, 41, "-1001", "course:7:Fall 2026",
                f"Question {index}", f"Answer {index}", index * 100,
            )

        await service.save_tutor_exchange(
            database, 42, "-1001", "course:7:Fall 2026", "Other user", "Other answer", 2_000
        )
        await service.save_tutor_exchange(
            database, 41, "-1001", "course:8:Fall 2026", "Other course", "Other answer", 2_100
        )
        await service.save_tutor_exchange(
            database, 41, "41", service.PRIVATE_TUTOR_CONTEXT, "Private", "Private answer", 2_200
        )

        history = await service.load_tutor_memory(
            database, 41, "-1001", "course:7:Fall 2026"
        )
        expected_history = []
        for index in range(2, 10):
            expected_history.extend([
                {"role": "user", "content": f"Question {index}"},
                {"role": "assistant", "content": f"Answer {index}"},
            ])
        assert history == expected_history
        assert len(history) == 16
        assert history[0] == {"role": "user", "content": "Question 2"}
        assert history[-1] == {"role": "assistant", "content": "Answer 9"}
        assert all("Other" not in row["content"] and "Private" not in row["content"] for row in history)

        await service.forget_tutor_user(database, 41)
        assert await service.get_tutor_preferences(database, 41) == ""
        assert await service.load_tutor_memory(database, 41, "-1001", "course:7:Fall 2026") == []
        assert await service.load_tutor_memory(database, 42, "-1001", "course:7:Fall 2026") == [
            {"role": "user", "content": "Other user"},
            {"role": "assistant", "content": "Other answer"},
        ]

    asyncio.run(exercise())


def test_source_text_is_split_into_bounded_overlapping_passages():
    service = load_service()
    source = "0123456789" * 20

    passages = service.chunk_source_text(source, chunk_size=80, overlap=20)

    assert len(passages) == 4
    assert all(len(passage) <= 80 for passage in passages)
    assert passages[0][-20:] == passages[1][:20]
    assert passages[1][-20:] == passages[2][:20]


def test_note_citation_link_is_built_from_supergroup_and_message_ids_when_missing():
    service = load_service()

    assert service.telegram_message_link("-100123456", 900) == "https://t.me/c/123456/900"
    assert service.telegram_message_link("-123456", 900) is None
    assert service.telegram_message_link("-100123456", 900, "https://t.me/c/123456/901") == "https://t.me/c/123456/901"


def test_general_tutor_still_answers_when_live_search_is_unavailable(monkeypatch):
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    calls = []

    async def unavailable_search(question, api_key, **_kwargs):
        raise RuntimeError("search unavailable")

    def build_messages(question, web_sources, **kwargs):
        calls.append((question, web_sources, kwargs))
        return [{"role": "user", "content": question}]

    async def generate_answer(ai_binding, messages):
        return "A recursion base case stops the function."

    monkeypatch.setattr(service.course_tutor_ai, "search_web", unavailable_search)
    monkeypatch.setattr(service.course_tutor_ai, "build_general_tutor_messages", build_messages)
    monkeypatch.setattr(service.course_tutor_ai, "generate_tutor_answer", generate_answer)

    answer = asyncio.run(service.answer_general_question(
        database, object(), "", "77", 77, "Explain recursion", 1_800_000_000_000
    ))

    assert "A recursion base case" in answer
    assert "without live citations" in answer
    assert calls == [("Explain recursion", [], {
        "conversation_history": [], "study_preferences": "", "attachment_context": None,
    })]


def test_general_tutor_skips_search_and_sources_for_smalltalk(monkeypatch):
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    searches = []

    async def search_web(*args):
        searches.append(args)
        return []

    monkeypatch.setattr(service.course_tutor_ai, "search_web", search_web)
    monkeypatch.setattr(
        service.course_tutor_ai,
        "build_general_tutor_messages",
        lambda question, sources, **kwargs: [{"role": "user", "content": question}],
    )
    async def generate(*_args):
        return "I'm doing well, thanks! How about you?"

    monkeypatch.setattr(service.course_tutor_ai, "generate_tutor_answer", generate)
    answer = asyncio.run(service.answer_general_question(
        database, object(), "", "77", 77, "How are you?", 1_800_000_000_000
    ))

    assert searches == []
    assert "I'm doing well, thanks!" in answer
    assert "Additional sources" not in answer
    assert "reliable outside sources" not in answer


def test_general_tutor_remembers_only_same_users_private_conversation(monkeypatch):
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    generated = []

    async def no_search(_question, _api_key):
        return []

    def build_messages(question, sources, **kwargs):
        generated.append((question, kwargs["conversation_history"], kwargs["study_preferences"]))
        return [{"role": "user", "content": question}]

    async def generate(_binding, _messages):
        return f"Answer {len(generated)}"

    monkeypatch.setattr(service.course_tutor_ai, "should_search_web", lambda _question: False)
    monkeypatch.setattr(service.course_tutor_ai, "search_web", no_search)
    monkeypatch.setattr(service.course_tutor_ai, "build_general_tutor_messages", build_messages)
    monkeypatch.setattr(service.course_tutor_ai, "generate_tutor_answer", generate)

    async def exercise():
        await service.set_tutor_preferences(database, 11, "Use short examples", 1)
        await service.answer_general_question(database, object(), "", "11", 11, "First question", 100)
        await service.answer_general_question(database, object(), "", "11", 11, "Follow-up", 200)
        await service.answer_general_question(database, object(), "", "12", 12, "Other student", 300)

    asyncio.run(exercise())
    assert generated[0] == ("First question", [], "Use short examples")
    assert generated[1][0] == "Follow-up"
    assert generated[1][1] == [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "Answer 1"},
    ]
    assert generated[2] == ("Other student", [], "")


def test_general_tutor_answers_when_memory_read_or_write_fails(monkeypatch):
    import asyncio

    service = load_service()
    database = SqliteDatabase()

    async def broken_read(*_args):
        raise RuntimeError("D1 temporarily unavailable")

    async def broken_write(*_args):
        raise RuntimeError("D1 temporarily unavailable")

    monkeypatch.setattr(service, "get_tutor_preferences", broken_read)
    monkeypatch.setattr(service, "load_tutor_memory", broken_read)
    monkeypatch.setattr(service, "save_tutor_exchange", broken_write)
    monkeypatch.setattr(service.course_tutor_ai, "should_search_web", lambda _question: False)
    monkeypatch.setattr(service.course_tutor_ai, "build_general_tutor_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(service.course_tutor_ai, "generate_tutor_answer", lambda *_args: _async_value("A helpful answer."))

    answer = asyncio.run(service.answer_general_question(
        database, object(), "", "11", 11, "Question", 100
    ))
    assert "A helpful answer." in answer


def test_course_tutor_memory_isolated_per_user_and_course_topic(monkeypatch):
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    database.seed_course()
    asyncio.run(service.index_ready_source(database, 1, 50))
    generated = []

    async def no_search(_question, _api_key):
        return []

    async def course_passages(*_args, **_kwargs):
        return [{"source_id": 1, "title": "Notes", "passage_text": "Source material", "message_link": None}]

    def build_messages(_name, _semester, question, passages, sources, **kwargs):
        generated.append((question, kwargs["conversation_history"]))
        return [{"role": "user", "content": question}]

    async def generate(_binding, _messages):
        return f"Course answer {len(generated)} [M1]"

    monkeypatch.setattr(service.course_tutor_ai, "should_search_web", lambda _question: False)
    monkeypatch.setattr(service.course_tutor_ai, "search_web", no_search)
    monkeypatch.setattr(service, "retrieve_course_passages", course_passages)
    monkeypatch.setattr(service.course_tutor_ai, "build_tutor_messages", build_messages)
    monkeypatch.setattr(service.course_tutor_ai, "generate_tutor_answer", generate)

    async def exercise():
        await service.answer_course_question(database, object(), "", "-1001", 42, 51, "First course question", 100)
        await service.answer_course_question(database, object(), "", "-1001", 42, 51, "Course follow-up", 200)
        await service.answer_course_question(database, object(), "", "-1001", 42, 52, "Another student", 300)

    asyncio.run(exercise())
    assert generated[0] == ("First course question", [])
    assert generated[1][0] == "Course follow-up"
    assert generated[1][1] == [
        {"role": "user", "content": "First course question"},
        {"role": "assistant", "content": "Course answer 1 [M1]"},
    ]
    assert generated[2] == ("Another student", [])

    context = {"course_id": 7, "thread_id": 42, "semester_label": "Fall 2026"}
    assert service.course_tutor_context_key(context) == "course:7:42:Fall 2026"
    assert service.course_tutor_context_key({**context, "thread_id": 43}) != service.course_tutor_context_key(context)
    assert service.course_tutor_context_key({**context, "semester_label": "Spring 2027"}) != service.course_tutor_context_key(context)


async def _async_value(value):
    return value


def test_indexing_is_idempotent_and_retrieval_stays_in_current_course_semester():
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    course_id = database.seed_course()
    other_course_id = database.connection.execute(
        "INSERT INTO courses (college_id, chat_id, course_name, normalized_name, created_at_ms) VALUES ('college-a', '-1001', 'Physics', 'physics', 1) RETURNING id"
    ).fetchone()[0]
    database.connection.execute(
        "INSERT INTO course_offerings (college_id, chat_id, course_id, semester_label, created_at_ms) VALUES ('college-a', '-1001', ?, 'Spring 2026', 1)",
        (other_course_id,),
    )
    database.connection.execute(
        "INSERT INTO course_quiz_sources (college_id, chat_id, course_id, semester_label, thread_id, source_message_id, resource_type, title, extracted_text, extraction_state, created_at_ms, updated_at_ms) VALUES ('college-a', '-1001', ?, 'Spring 2026', 43, 901, 'note', 'Relational algebra old course', 'Relational algebra should not cross semester boundaries.', 'ready', 1, 1)",
        (other_course_id,),
    )
    database.connection.commit()

    assert asyncio.run(service.index_ready_source(database, 1, 1000)) is True
    assert asyncio.run(service.index_ready_source(database, 1, 2000)) is True
    results = asyncio.run(
        service.retrieve_course_passages(database, "-1001", 42, "relational algebra selection", limit=5)
    )

    assert results
    assert {row["source_message_id"] for row in results} == {900}
    assert all(row["semester_label"] == "Fall 2026" for row in results)
    assert database.connection.execute("SELECT COUNT(*) FROM course_tutor_passages").fetchone()[0] > 0
    assert database.connection.execute(
        "SELECT COUNT(*) FROM course_tutor_passages WHERE source_id = 1"
    ).fetchone()[0] == len(service.chunk_source_text(
        database.connection.execute("SELECT extracted_text FROM course_quiz_sources WHERE id = 1").fetchone()[0]
    ))


def test_index_tick_claims_a_queued_source_and_marks_it_indexed():
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    database.seed_course()

    assert asyncio.run(service.process_next_index_job(database, 1000)) is True
    assert database.connection.execute(
        "SELECT status FROM course_tutor_index_jobs WHERE source_id = 1"
    ).fetchone()[0] == "indexed"
    assert database.connection.execute(
        "SELECT COUNT(*) FROM course_tutor_passages WHERE source_id = 1"
    ).fetchone()[0] > 0
    assert asyncio.run(service.process_next_index_job(database, 2000)) is False


def test_tutor_budget_caps_each_user_and_group_per_day_without_storing_questions():
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    database.seed_course()

    user_results = [
        asyncio.run(service.consume_tutor_request_budget(database, "-1001", 77, 1_800_000_000_000))
        for _ in range(6)
    ]
    other_users = [
        asyncio.run(service.consume_tutor_request_budget(database, "-1001", user_id, 1_800_000_000_000))
        for user_id in range(78, 100)
    ]

    assert user_results == [True] * 6
    assert sum(other_users) == 22
    assert database.connection.execute(
        "SELECT SUM(request_count) FROM tutor_ai_daily_usage WHERE chat_id = '-1001'"
    ).fetchone()[0] == 28
    assert "question" not in " ".join(row[1] for row in database.connection.execute("PRAGMA table_info(tutor_ai_daily_usage)"))


def test_tutor_budget_reserves_global_monthly_search_allowance():
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    database.seed_course()
    group_ids = ["-1001"]
    for index in range(2, 42):
        chat_id = f"-100{index}"
        database.connection.execute(
            "INSERT INTO academic_groups (chat_id, college_id, group_title, created_by_user_id, created_at_ms) VALUES (?, 'college-a', 'Pilot group', 1, 1)",
            (chat_id,),
        )
        group_ids.append(chat_id)
    database.connection.commit()

    consumed = 0
    for group_index, chat_id in enumerate(group_ids[:30]):
        for request_index in range(100):
            user_id = 1_000 + group_index * 100 + request_index
            if asyncio.run(service.consume_tutor_request_budget(database, chat_id, user_id, 1_800_000_000_000)):
                consumed += 1
    final_request = asyncio.run(
        service.consume_tutor_request_budget(database, group_ids[40], 9_999, 1_800_000_000_000)
    )

    assert consumed == 3_000
    assert final_request is False


def test_course_context_cannot_be_resolved_from_general_topic():
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    database.seed_course()

    context = asyncio.run(service.resolve_course_context(database, "-1001", None))

    assert context is None


def test_tutor_passages_are_deduplicated_by_original_material_before_citation_numbering():
    service = load_service()

    selected = service.unique_source_passages([
        {"source_id": 1, "title": "Chapter 1", "passage_text": "First passage"},
        {"source_id": 1, "title": "Chapter 1", "passage_text": "Second passage"},
        {"source_id": 2, "title": "Lecture 2", "passage_text": "Third passage"},
    ])

    assert [row["source_id"] for row in selected] == [1, 2]
    assert [row["passage_text"] for row in selected] == ["First passage", "Third passage"]


def test_answer_uses_course_materials_and_live_sources_with_clear_source_links(monkeypatch):
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    database.seed_course()
    asyncio.run(service.index_ready_source(database, 1, 1000))
    web_calls = []

    async def search_web(question, api_key, **_kwargs):
        web_calls.append((question, api_key))
        return [{"title": "Database textbook", "url": "https://example.edu/db", "content": "Normalization reduces duplication."}]

    monkeypatch.setattr(service.course_tutor_ai, "search_web", search_web)

    class AI:
        async def run(self, _model, options):
            assert "course materials" in options["messages"][0]["content"]
            assert "[M1]" in options["messages"][1]["content"]
            assert "[W1]" in options["messages"][1]["content"]
            return {"response": "Normalization reduces duplication [M1] and improves structure [W1]."}

    answer = asyncio.run(service.answer_course_question(
        database, AI(), "tavily-secret", "-1001", 42, 77,
        "Explain normalization", 1_800_000_000_000,
    ))

    assert web_calls == [("Explain normalization", "tavily-secret")]
    assert "I found this in your course materials (Databases)" in answer
    assert '<a href="https://t.me/c/1001/900">Relational algebra</a>' in answer
    assert '<a href="https://example.edu/db">Database textbook</a>' in answer
    assert "[1]" not in answer
    assert "Additional sources" in answer


def test_answer_states_when_course_material_is_missing_and_uses_only_global_sources(monkeypatch):
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    database.seed_course()

    async def search_web(_question, _api_key, **_kwargs):
        return [{"title": "Database textbook", "url": "https://example.edu/db", "content": "A definition."}]

    monkeypatch.setattr(service.course_tutor_ai, "search_web", search_web)

    class AI:
        async def run(self, _model, _options):
            return {"response": "A database organizes related records [W1]."}

    answer = asyncio.run(service.answer_course_question(
        database, AI(), "tavily-secret", "-1001", 42, 77,
        "Explain databases", 1_800_000_000_000,
    ))

    assert "couldn't find this in the materials" in answer.lower()
    assert "I found this in your course materials" not in answer
    assert '<a href="https://example.edu/db">Database textbook</a>' in answer
    assert "[1]" not in answer


def test_course_only_fallback_discloses_when_live_search_is_unavailable(monkeypatch):
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    database.seed_course()
    asyncio.run(service.index_ready_source(database, 1, 1000))

    async def fail_search(_question, _api_key, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(service.course_tutor_ai, "search_web", fail_search)

    class AI:
        async def run(self, _model, _options):
            return {"response": "Relational algebra derives relations from database tables [M1]."}

    answer = asyncio.run(service.answer_course_question(
        database, AI(), "secret", "-1001", 42, 77,
        "Explain relational algebra", 1_800_000_000_000,
    ))

    assert "I found this in your course materials" in answer
    assert "Live web search isn't configured" in answer


def test_course_only_answer_discloses_when_web_search_returns_no_sources(monkeypatch):
    import asyncio

    service = load_service()
    database = SqliteDatabase()
    database.seed_course()
    asyncio.run(service.index_ready_source(database, 1, 1000))

    async def no_results(_question, _api_key, **_kwargs):
        return []

    monkeypatch.setattr(service.course_tutor_ai, "search_web", no_results)

    class AI:
        async def run(self, _model, _options):
            return {"response": "Relational algebra derives relations [M1]."}

    answer = asyncio.run(service.answer_course_question(
        database, AI(), "secret", "-1001", 42, 77,
        "Explain relational algebra", 1_800_000_000_000,
    ))

    assert "I found this in your course materials" in answer
    assert "No reliable outside sources were found" in answer
