"""Middleware registration."""

from aiogram import Dispatcher

from app.bot.middlewares.error import GlobalErrorMiddleware
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.logging import CallbackTraceMiddleware, LoggingMiddleware
from app.bot.middlewares.throttle import ThrottleMiddleware


def register_all_middlewares(dp: Dispatcher) -> None:
    """Register all middlewares on the dispatcher."""
    # Catch ALL exceptions from any handler or middleware under it
    dp.update.outer_middleware(GlobalErrorMiddleware())
    # Order matters: logging next, then throttle, then DB session
    dp.update.outer_middleware(LoggingMiddleware())
    dp.message.outer_middleware(ThrottleMiddleware())
    dp.callback_query.middleware(CallbackTraceMiddleware())
    dp.message.middleware(DbSessionMiddleware())
    dp.callback_query.middleware(DbSessionMiddleware())
