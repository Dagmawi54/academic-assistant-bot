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
    """Run in polling mode (development or free-tier hosting)."""
    from app.bot.webhook import app as fastapi_app
    from app.bot import bot, dp
    from app.bot.webhook import lifespan
    import os

    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)

    # Manually trigger lifespan startup
    async with lifespan(fastapi_app):
        print("🤖 Bot running in polling mode alongside FastAPI server. Press Ctrl+C to stop.")
        
        polling_task = asyncio.create_task(dp.start_polling(bot, drop_pending_updates=True))
        
        try:
            await server.serve()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            polling_task.cancel()


if __name__ == "__main__":
    main()
