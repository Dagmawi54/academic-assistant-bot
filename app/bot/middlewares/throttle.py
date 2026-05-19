"""Per-user throttle middleware to prevent spam."""

import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message

from app.config import settings
from app.logging import get_logger

logger = get_logger("throttle")


class ThrottleMiddleware(BaseMiddleware):
    """Rate-limits messages per user to prevent spam flooding."""

    def __init__(self) -> None:
        self._timestamps: dict[int, float] = defaultdict(float)

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        if not event.from_user:
            return await handler(event, data)

        user_id = event.from_user.id
        now = time.monotonic()
        last = self._timestamps.get(user_id, 0.0)

        if now - last < settings.throttle_rate:
            logger.debug("throttled", user_id=user_id)
            return None  # Silently drop

        self._timestamps[user_id] = now
        return await handler(event, data)
