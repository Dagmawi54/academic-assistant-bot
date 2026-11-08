import httpx
import pytest


@pytest.mark.asyncio
async def test_webhook_rejects_missing_or_wrong_secret(monkeypatch):
    from app.bot import dp
    from app.bot.webhook import app
    from app.config import settings

    async def unexpected_update(*_args, **_kwargs):
        pytest.fail("an unauthorized update must not reach the dispatcher")

    monkeypatch.setattr(dp, "feed_update", unexpected_update)
    monkeypatch.setattr(settings, "use_polling", False)
    monkeypatch.setattr(settings, "webhook_secret", "expected-secret")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        missing = await client.post("/webhook", json={"update_id": 1})
        wrong = await client.post(
            "/webhook",
            json={"update_id": 2},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        )

    assert missing.status_code == 403
    assert wrong.status_code == 403


@pytest.mark.asyncio
async def test_webhook_fails_closed_when_secret_is_not_configured(monkeypatch):
    from app.bot import dp
    from app.bot.webhook import app
    from app.config import settings

    async def unexpected_update(*_args, **_kwargs):
        pytest.fail("the webhook must not accept updates without a configured secret")

    monkeypatch.setattr(dp, "feed_update", unexpected_update)
    monkeypatch.setattr(settings, "use_polling", False)
    monkeypatch.setattr(settings, "webhook_secret", "")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/webhook", json={"update_id": 4})

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_webhook_accepts_valid_secret(monkeypatch):
    from app.bot import dp
    from app.bot.webhook import app
    from app.config import settings

    updates = []

    async def record_update(**kwargs):
        updates.append(kwargs["update"])

    monkeypatch.setattr(dp, "feed_update", record_update)
    monkeypatch.setattr(settings, "use_polling", False)
    monkeypatch.setattr(settings, "webhook_secret", "expected-secret")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/webhook",
            json={"update_id": 3},
            headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
        )

    assert response.status_code == 200
    assert len(updates) == 1
    assert updates[0].update_id == 3
