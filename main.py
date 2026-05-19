"""Application entrypoint — starts FastAPI with uvicorn or polling mode."""

import asyncio
import uvicorn

from app.config import settings


def main() -> None:
    """Start the application in either webhook or polling mode."""
    if settings.use_polling:
        # Polling mode: run bot polling alongside FastAPI health endpoint
        asyncio.run(_run_polling())
    else:
        # Webhook mode: just start FastAPI (webhook handler included)
        uvicorn.run(
            "app.bot.webhook:app",
            host="0.0.0.0",
            port=8000,
            log_level="info",
        )


async def _run_polling() -> None:
    """Run in polling mode (development)."""
    from app.bot.webhook import app as fastapi_app
    from app.bot import bot, dp
    from app.bot.webhook import lifespan

    # Manually trigger lifespan startup
    async with lifespan(fastapi_app):
        # Start polling
        print("🤖 Bot running in polling mode. Press Ctrl+C to stop.")
        try:
            await dp.start_polling(bot, drop_pending_updates=True)
        except (KeyboardInterrupt, SystemExit):
            pass


if __name__ == "__main__":
    main()
