"""Self-ping keep-alive to prevent Render free-tier from sleeping.

Render free-tier services sleep after ~15 min of inactivity.
This module spawns a lightweight asyncio task that pings the
service's own /health endpoint every 14 minutes.
"""

import asyncio
import httpx

from app.logging import get_logger

logger = get_logger("keep_alive")

_task: asyncio.Task | None = None
PING_INTERVAL = 14 * 60  # 14 minutes (just under the 15-min limit)


async def _ping_loop(url: str) -> None:
    """Continuously GET the health endpoint."""
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                resp = await client.get(url)
                logger.info("keep_alive_ping", status=resp.status_code, url=url)
            except Exception:
                logger.warning("keep_alive_ping_failed", url=url)
            await asyncio.sleep(PING_INTERVAL)


def start_keep_alive(external_url: str | None) -> None:
    """Start the background ping task if an external URL is configured."""
    global _task

    if not external_url:
        logger.info("keep_alive_disabled", reason="No RENDER_EXTERNAL_URL set")
        return

    health_url = f"{external_url.rstrip('/')}/health"
    _task = asyncio.create_task(_ping_loop(health_url))
    logger.info("keep_alive_started", url=health_url, interval_sec=PING_INTERVAL)


def stop_keep_alive() -> None:
    """Cancel the ping task on shutdown."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        logger.info("keep_alive_stopped")
