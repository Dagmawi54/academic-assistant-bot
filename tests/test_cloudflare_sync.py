import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import CloudflareSyncOutbox
from app.integrations import cloudflare_sync
from app.integrations.cloudflare_sync import serialize_academic_item


def test_academic_item_payload_preserves_schedule_fields_and_normalizes_legacy_status():
    addis = ZoneInfo("Africa/Addis_Ababa")
    deadline = datetime(2027, 1, 15, 9, 30, tzinfo=addis)
    send_time = datetime(2027, 1, 14, 9, 30, tzinfo=addis)
    item = SimpleNamespace(
        id=123,
        group_id=8,
        source_chat_id=-100123,
        item_type="EXAM",
        title="Final",
        deadline=deadline,
        room="B-12",
        coverage="Chapters 1–4",
        status="approved",
    )
    reminder = SimpleNamespace(
        id=456,
        thread_id=42,
        send_time=send_time,
        sent=False,
        cancelled=False,
    )

    payload = serialize_academic_item(item, [reminder])

    assert payload == {
        "source_item_id": 123,
        "group_id": 8,
        "chat_id": "-100123",
        "thread_id": 42,
        "item_type": "exam",
        "title": "Final",
        "deadline_ms": int(deadline.timestamp() * 1000),
        "room": "B-12",
        "coverage": "Chapters 1–4",
        "status": "active",
        "reminders": [
            {
                "source_reminder_id": 456,
                "send_time_ms": int(send_time.timestamp() * 1000),
                "sent": False,
                "cancelled": False,
            }
        ],
    }


def test_worker_sync_failure_is_persisted_and_retried(monkeypatch):
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(CloudflareSyncOutbox.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(cloudflare_sync.settings, "cloudflare_worker_url", "https://worker.test")
        monkeypatch.setattr(cloudflare_sync.settings, "cloudflare_sync_secret", "local-test-secret")
        calls = []

        async def fail_once(items):
            calls.append(items)
            return len(calls) > 1

        monkeypatch.setattr(cloudflare_sync, "_post_items", fail_once)

        async with factory() as session:
            session.add(
                CloudflareSyncOutbox(
                    academic_item_id=321,
                    payload='{"source_item_id": 321}',
                )
            )
            await session.commit()

            session.info["cloudflare_academic_item_ids"] = {321}
            await cloudflare_sync.flush_queued_items(session)
            row = await session.get(CloudflareSyncOutbox, 1)
            assert row.status == "pending"
            assert row.attempt_count == 1
            assert row.last_error == "worker_sync_failed"

            row.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()
            session.info["cloudflare_academic_item_ids"] = {321}
            await cloudflare_sync.flush_queued_items(session)
            await session.refresh(row)
            assert row.status == "synced"
            assert row.attempt_count == 2
            assert row.last_error is None

        assert len(calls) == 2
        await engine.dispose()

    asyncio.run(run())
