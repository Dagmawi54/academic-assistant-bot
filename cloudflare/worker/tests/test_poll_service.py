import importlib.util
import sqlite3
from pathlib import Path


def load_poll_service():
    service_path = Path(__file__).parents[1] / "src" / "poll_service.py"
    spec = importlib.util.spec_from_file_location("cloudflare_poll_service_under_test", service_path)
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    return service


def test_parse_class_poll_accepts_two_to_ten_clean_options():
    service = load_poll_service()

    assert service.parse_class_poll_command(
        "/class_poll@academicgroupmanagementbot Which topic next? | Databases | Networks | Security"
    ) == {
        "question": "Which topic next?",
        "options": ["Databases", "Networks", "Security"],
    }


def test_parse_class_poll_rejects_bad_counts_duplicates_and_oversized_fields():
    service = load_poll_service()

    assert service.parse_class_poll_command("/class_poll Q | Only one") is None
    assert service.parse_class_poll_command("/class_poll Q | Same | same") is None
    assert service.parse_class_poll_command("/class_poll " + "Q" * 301 + " | A | B") is None
    assert service.parse_class_poll_command("/class_poll Q | " + "A" * 101 + " | B") is None
    assert service.parse_class_poll_command("/class_poll Q | " + " | ".join(str(i) for i in range(11))) is None


class PollStatement:
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

    async def run(self):
        cursor = self.database.connection.execute(self.query, self.values)
        self.database.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class PollDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[1] / "migrations" / "0010_class_poll_receipts.sql"
        self.connection.executescript(migration.read_text(encoding="utf-8"))

    def prepare(self, query):
        return PollStatement(self, query)


def test_poll_receipt_allows_one_send_attempt_per_telegram_update():
    import asyncio

    service = load_poll_service()
    database = PollDatabase()

    assert asyncio.run(service.claim_class_poll(database, 100, "-100123", 1000)) is True
    assert asyncio.run(service.claim_class_poll(database, 100, "-100123", 2000)) is False
    asyncio.run(service.finish_class_poll(database, 100, "sent", 1100, "poll-1"))

    row = database.connection.execute(
        "SELECT chat_id, status, poll_id FROM class_poll_receipts WHERE update_id = 100"
    ).fetchone()
    assert dict(row) == {"chat_id": "-100123", "status": "sent", "poll_id": "poll-1"}
