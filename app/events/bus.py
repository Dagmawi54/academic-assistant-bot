"""Lightweight async event bus for decoupled message handling."""

import asyncio
from collections import defaultdict
from typing import Any, Callable, Coroutine

from app.logging import get_logger

logger = get_logger("events")

# Event handler type
EventHandler = Callable[..., Coroutine[Any, Any, None]]

# Global registry
_handlers: dict[str, list[EventHandler]] = defaultdict(list)


# ---------------------------------------------------------------------------
# Event names
# ---------------------------------------------------------------------------

ASSIGNMENT_DETECTED = "assignment_detected"
EXAM_DETECTED = "exam_detected"
ITEM_UPDATED = "item_updated"
SEMESTER_CLOSED = "semester_closed"
SEMESTER_ACTIVATED = "semester_activated"
ADMIN_ACTION = "admin_action"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def on(event_name: str) -> Callable[[EventHandler], EventHandler]:
    """Decorator to register an event handler.

    Usage:
        @on("assignment_detected")
        async def handle_assignment(item_id: int, **kwargs):
            ...
    """

    def decorator(func: EventHandler) -> EventHandler:
        _handlers[event_name].append(func)
        return func

    return decorator


async def emit(event_name: str, **kwargs: Any) -> None:
    """Emit an event — all registered handlers run concurrently."""
    handlers = _handlers.get(event_name, [])
    if not handlers:
        return

    logger.info("event_emitted", event=event_name, handler_count=len(handlers))

    tasks = [asyncio.create_task(_safe_call(h, event_name, **kwargs)) for h in handlers]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _safe_call(handler: EventHandler, event_name: str, **kwargs: Any) -> None:
    """Call a handler with error suppression so one failure doesn't block others."""
    try:
        await handler(**kwargs)
    except Exception:
        logger.exception("event_handler_failed", event=event_name, handler=handler.__name__)
