import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import group_setup_service as service

MIGRATION = ROOT / "migrations" / "0017_guided_group_setup.sql"


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE colleges (college_id TEXT PRIMARY KEY);
        CREATE TABLE academic_groups (
            chat_id TEXT PRIMARY KEY,
            college_id TEXT NOT NULL REFERENCES colleges(college_id)
        );
        CREATE TABLE courses (
            id INTEGER PRIMARY KEY,
            college_id TEXT NOT NULL REFERENCES colleges(college_id),
            chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id)
        );
        CREATE TABLE group_topics (
            chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id),
            thread_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL REFERENCES courses(id),
            PRIMARY KEY (chat_id, thread_id)
        );
    """)
    connection.executescript(MIGRATION.read_text(encoding="utf-8"))
    yield connection
    connection.close()


def test_group_setup_schema_has_topic_observation_and_run_journal(database):
    tables = {
        row[0]
        for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    indexes = {
        row[0]
        for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }

    assert {"observed_group_topics", "group_setup_runs", "group_setup_changes"} <= tables
    assert "ix_observed_group_topics_chat_time" in indexes
    assert "ix_group_setup_runs_latest" in indexes


def test_group_setup_change_journal_distinguishes_new_mapping_from_remap(database):
    database.execute("INSERT INTO colleges VALUES ('college-1')")
    database.execute("INSERT INTO academic_groups (chat_id, college_id) VALUES ('-1001', 'college-1')")
    database.execute("INSERT INTO courses VALUES (1, 'college-1', '-1001')")
    database.execute("INSERT INTO courses VALUES (2, 'college-1', '-1001')")
    database.execute(
        "INSERT INTO group_setup_runs (id, chat_id, college_id, semester_label, started_by_user_id, status, created_at_ms) "
        "VALUES (1, '-1001', 'college-1', 'Fall 2026', 10, 'reviewing', 1)"
    )
    database.execute(
        "INSERT INTO group_setup_changes (run_id, chat_id, thread_id, previous_course_id, applied_course_id, applied_by_user_id, created_at_ms) "
        "VALUES (1, '-1001', 50, NULL, 1, 10, 1)"
    )
    database.execute(
        "INSERT INTO group_setup_changes (run_id, chat_id, thread_id, previous_course_id, applied_course_id, applied_by_user_id, created_at_ms) "
        "VALUES (1, '-1001', 51, 1, 2, 10, 1)"
    )

    rows = database.execute(
        "SELECT previous_course_id, applied_course_id FROM group_setup_changes ORDER BY thread_id"
    ).fetchall()
    assert rows == [(None, 1), (1, 2)]


def test_observed_topic_is_unique_per_group_and_run_status_is_constrained(database):
    database.execute("INSERT INTO colleges VALUES ('college-1')")
    database.execute("INSERT INTO academic_groups (chat_id, college_id) VALUES ('-1001', 'college-1')")
    database.execute(
        "INSERT INTO observed_group_topics (chat_id, thread_id, topic_title, observed_at_ms) VALUES ('-1001', 50, 'Data Structures', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO observed_group_topics (chat_id, thread_id, topic_title, observed_at_ms) VALUES ('-1001', 50, 'Different title', 2)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO group_setup_runs (chat_id, college_id, semester_label, started_by_user_id, status, created_at_ms) "
            "VALUES ('-1001', 'college-1', 'Fall 2026', 10, 'broken', 1)"
        )


def test_exact_topic_title_match_is_the_only_auto_apply_case():
    decisions = service.classify_topic_matches(
        [
            {"thread_id": 11, "topic_title": "Operating Systems"},
            {"thread_id": 12, "topic_title": "Operating System"},
        ],
        [
            {"course_id": 5, "course_name": " operating   systems ", "active": 1},
            {"course_id": 6, "course_name": "Computer Networks", "active": 1},
        ],
    )

    assert decisions[0]["status"] == "auto_apply"
    assert decisions[0]["course_id"] == 5
    assert decisions[1]["status"] == "needs_admin_choice"


def test_ambiguous_or_conflicting_exact_match_never_auto_applies():
    ambiguous = service.classify_topic_matches(
        [{"thread_id": 20, "topic_title": "Calculus II"}],
        [
            {"course_id": 1, "course_name": "Calculus II", "active": 1},
            {"course_id": 2, "course_name": "Calculus II", "active": 1},
        ],
    )
    conflict = service.classify_topic_matches(
        [{"thread_id": 21, "topic_title": "Calculus II", "current_course_id": 3}],
        [{"course_id": 1, "course_name": "Calculus II", "active": 1}],
    )

    assert ambiguous[0]["status"] == "needs_admin_choice"
    assert conflict[0]["status"] == "needs_admin_choice"


class _Statement:
    def __init__(self, connection, query):
        self.connection = connection
        self.query = query
        self.values = ()

    def bind(self, *values):
        self.values = values
        return self

    async def first(self):
        row = self.connection.execute(self.query, self.values).fetchone()
        return dict(row) if row is not None else None

    async def all(self):
        rows = self.connection.execute(self.query, self.values).fetchall()
        return {"results": [dict(row) for row in rows]}

    async def run(self):
        cursor = self.connection.execute(self.query, self.values)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class _D1:
    def __init__(self, connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def prepare(self, query):
        return _Statement(self.connection, query)

    async def batch(self, statements):
        with self.connection:
            return [self.connection.execute(statement.query, statement.values) for statement in statements]


@pytest.fixture
def populated_database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE colleges (college_id TEXT PRIMARY KEY, college_name TEXT NOT NULL);
        CREATE TABLE academic_groups (
            chat_id TEXT PRIMARY KEY, college_id TEXT NOT NULL REFERENCES colleges(college_id),
            group_title TEXT, active INTEGER NOT NULL DEFAULT 1, current_semester TEXT
        );
        CREATE TABLE courses (
            id INTEGER PRIMARY KEY, college_id TEXT NOT NULL REFERENCES colleges(college_id),
            chat_id TEXT NOT NULL REFERENCES academic_groups(chat_id), course_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at_ms INTEGER NOT NULL
        );
        CREATE TABLE course_offerings (
            college_id TEXT NOT NULL, chat_id TEXT NOT NULL, course_id INTEGER NOT NULL,
            semester_label TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at_ms INTEGER NOT NULL,
            PRIMARY KEY (chat_id, course_id, semester_label)
        );
        CREATE TABLE group_topics (
            chat_id TEXT NOT NULL, thread_id INTEGER NOT NULL, course_id INTEGER NOT NULL,
            created_by_user_id INTEGER NOT NULL, created_at_ms INTEGER NOT NULL,
            PRIMARY KEY (chat_id, thread_id)
        );
    """)
    connection.executescript(MIGRATION.read_text(encoding="utf-8"))
    connection.execute("INSERT INTO colleges VALUES ('college-1', 'College')")
    connection.execute("INSERT INTO academic_groups VALUES ('-1001', 'college-1', 'Class', 1, 'Fall 2026')")
    connection.execute("INSERT INTO courses VALUES (1, 'college-1', '-1001', 'Operating Systems', 'operating systems', 1, 1)")
    connection.execute("INSERT INTO course_offerings VALUES ('college-1', '-1001', 1, 'Fall 2026', 1, 1)")
    connection.execute("INSERT INTO observed_group_topics VALUES ('-1001', 55, 'Operating Systems', 10, 2)")
    connection.commit()
    yield _D1(connection)
    connection.close()


def test_setup_state_scopes_known_topics_and_offerings_to_current_semester(populated_database):
    state = asyncio.run(service.get_group_setup_state(populated_database, "-1001"))

    assert state["status"] == "ready"
    assert state["semester_label"] == "Fall 2026"
    assert state["college_id"] == "college-1"
    assert state["topics"][0]["thread_id"] == 55
    assert state["offerings"][0]["course_name"] == "Operating Systems"


def test_guided_setup_applies_exact_match_and_undo_restores_unmapped_topic(populated_database):
    async def scenario():
        run = await service.begin_setup_run(populated_database, "-1001", 10, 100)
        assert run["run_id"]
        exact = [decision for decision in run["decisions"] if decision["status"] == "auto_apply"]
        result = await service.apply_exact_matches(populated_database, run["run_id"], 10, exact, 200)
        assert result["applied_count"] == 1
        mapped = await populated_database.prepare(
            "SELECT course_id FROM group_topics WHERE chat_id = ? AND thread_id = ?"
        ).bind("-1001", 55).first()
        assert mapped["course_id"] == 1
        undone = await service.undo_latest_setup_run(populated_database, "-1001", 10, 300)
        assert undone["status"] == "undone"
        assert undone["undone_count"] == 1
        mapped = await populated_database.prepare(
            "SELECT course_id FROM group_topics WHERE chat_id = ? AND thread_id = ?"
        ).bind("-1001", 55).first()
        assert mapped is None

    asyncio.run(scenario())


def test_undo_refuses_to_overwrite_a_newer_topic_mapping(populated_database):
    async def scenario():
        populated_database.connection.execute(
            "INSERT INTO courses VALUES (2, 'college-1', '-1001', 'Databases', 'databases', 1, 1)"
        )
        populated_database.connection.execute(
            "INSERT INTO course_offerings VALUES ('college-1', '-1001', 2, 'Fall 2026', 1, 1)"
        )
        populated_database.connection.commit()
        run = await service.begin_setup_run(populated_database, "-1001", 10, 100)
        exact = [decision for decision in run["decisions"] if decision["status"] == "auto_apply"]
        await service.apply_exact_matches(populated_database, run["run_id"], 10, exact, 200)
        populated_database.connection.execute(
            "UPDATE group_topics SET course_id = 2, created_by_user_id = 99, created_at_ms = 250 "
            "WHERE chat_id = '-1001' AND thread_id = 55"
        )
        populated_database.connection.commit()

        undo = await service.undo_latest_setup_run(populated_database, "-1001", 10, 300)
        mapping = await populated_database.prepare(
            "SELECT course_id FROM group_topics WHERE chat_id = ? AND thread_id = ?"
        ).bind("-1001", 55).first()
        assert undo["status"] == "stale"
        assert mapping["course_id"] == 2

    asyncio.run(scenario())


def test_guided_setup_exact_apply_uncertain_choice_and_undo_are_one_audited_run(populated_database):
    async def scenario():
        populated_database.connection.execute(
            "INSERT INTO courses VALUES (2, 'college-1', '-1001', 'Databases', 'databases', 1, 1)"
        )
        populated_database.connection.execute(
            "INSERT INTO course_offerings VALUES ('college-1', '-1001', 2, 'Fall 2026', 1, 1)"
        )
        await service.observe_forum_topic(populated_database, "-1001", 56, "Lecture Notes", 10, 3)
        run = await service.begin_setup_run(populated_database, "-1001", 10, 100)
        exact = [item for item in run["decisions"] if item["status"] == "auto_apply"]
        uncertain = [item for item in run["decisions"] if item["status"] == "needs_admin_choice"]
        assert len(exact) == 1
        assert len(uncertain) == 1

        applied = await service.apply_exact_matches(populated_database, run["run_id"], 10, exact, 200)
        assert applied["applied_count"] == 1
        chosen = await service.resolve_topic_match(
            populated_database, run["run_id"], 56, 2, 10, 250
        )
        assert chosen is True
        changes = await populated_database.prepare(
            "SELECT thread_id, applied_course_id, applied_by_user_id FROM group_setup_changes "
            "WHERE run_id = ? ORDER BY thread_id"
        ).bind(run["run_id"]).all()
        assert [(row["thread_id"], row["applied_course_id"], row["applied_by_user_id"])
                for row in changes["results"]] == [(55, 1, 10), (56, 2, 10)]

        undone = await service.undo_latest_setup_run(populated_database, "-1001", 10, 300)
        assert undone == {"status": "undone", "undone_count": 2}
        mappings = await populated_database.prepare(
            "SELECT thread_id FROM group_topics WHERE chat_id = ? ORDER BY thread_id"
        ).bind("-1001").all()
        assert mappings["results"] == []

    asyncio.run(scenario())
