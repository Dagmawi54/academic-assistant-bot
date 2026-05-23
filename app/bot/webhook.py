"""FastAPI webhook routes and application lifecycle."""

from contextlib import asynccontextmanager

from aiogram import types
from fastapi import FastAPI, Request, Response

from app.bot import bot, dp
from app.config import settings
from app.database.session import init_db, close_db
from app.cache.redis_cache import init_cache, close_cache
from app.logging import get_logger, setup_logging

logger = get_logger("webhook")


@asynccontextmanager
async def lifespan(application: FastAPI):  # noqa: ANN201, ARG001
    """Startup and shutdown hooks."""
    # --- Startup ---
    setup_logging()
    logger.info("starting_up")

    # Database
    await init_db()
    logger.info("database_initialized")

    # Cache
    await init_cache()

    # Register handlers (import triggers registration)
    from app.bot.handlers import register_all_handlers

    register_all_handlers(dp)
    logger.info("handlers_registered")

    # Register middlewares
    from app.bot.middlewares import register_all_middlewares

    register_all_middlewares(dp)
    logger.info("middlewares_registered")

    # Register event listeners (import triggers @on decorators)
    import app.events.handlers  # noqa: F401

    # Scheduler
    from app.reminders.scheduler import start_scheduler

    await start_scheduler()
    logger.info("scheduler_started")

    # Keep-alive self-ping (prevents Render free-tier sleep)
    from app.utils.keep_alive import start_keep_alive

    start_keep_alive(settings.render_external_url)
    logger.info("keep_alive_checked")

    # Run startup diagnostics
    from app.services.startup_service import run_startup_diagnostics
    await run_startup_diagnostics(bot)
    logger.info("startup_diagnostics_complete")

    # Webhook or polling
    if settings.use_polling:
        logger.info("polling_mode", msg="Using polling (dev mode)")
        import asyncio
        polling_task = asyncio.create_task(dp.start_polling(bot, drop_pending_updates=True))
    else:
        webhook_url = settings.webhook_url
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        logger.info("webhook_set", url=webhook_url)

    yield

    # Cancel polling task on shutdown
    if settings.use_polling and polling_task:
        polling_task.cancel()

    # --- Shutdown ---
    logger.info("shutting_down")
    from app.reminders.scheduler import stop_scheduler
    from app.utils.keep_alive import stop_keep_alive

    stop_keep_alive()
    stop_scheduler()

    if not settings.use_polling:
        await bot.delete_webhook()

    await close_cache()
    await close_db()
    await bot.session.close()
    logger.info("shutdown_complete")


# FastAPI app with lifespan
app = FastAPI(title="Academic Bot", lifespan=lifespan)

from fastapi.responses import JSONResponse


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> Response:
    """Catch unhandled API exceptions and return structured JSON."""
    logger.exception("unhandled_api_error", path=request.url.path, error_type=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal Server Error", "detail": "An unexpected error occurred."},
    )


@app.post("/webhook")
async def telegram_webhook(request: Request) -> Response:
    """Receive Telegram updates via webhook."""
    update_data = await request.json()
    update = types.Update(**update_data)
    await dp.feed_update(bot=bot, update=update)
    return Response(status_code=200)


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint for uptime monitoring."""
    return {"status": "ok", "mode": "polling" if settings.use_polling else "webhook"}
