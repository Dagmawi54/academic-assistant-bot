import pytest


@pytest.mark.asyncio
async def test_processing_errors_propagate_to_transaction_middleware(monkeypatch):
    from app.services import routing_service

    async def fail_processing(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(routing_service, "_process_group_message_inner", fail_processing)

    with pytest.raises(RuntimeError, match="database write failed"):
        await routing_service.process_group_message(
            session=None,
            chat_id=-100123,
            thread_id=7,
            text="assignment due tomorrow",
            user_id=42,
            message_id=9001,
        )
