"""Lightweight Redis-backed observability metrics tracker."""

from typing import Dict
from app.cache import redis_cache
from app.logging import get_logger

logger = get_logger("metrics")


class MetricsTracker:
    """Tracks operational statistics in Redis.

    Tolerates Redis failures gracefully to avoid disrupting workflows.
    """

    PREFIX = "metrics:"

    async def _incr(self, name: str, amount: int = 1) -> None:
        if redis_cache._redis:
            try:
                await redis_cache._redis.incr(f"{self.PREFIX}{name}", amount)
            except Exception:
                logger.debug("metrics_incr_failed", key=name)

    async def _incr_float(self, name: str, amount: float) -> None:
        if redis_cache._redis:
            try:
                await redis_cache._redis.incrbyfloat(f"{self.PREFIX}{name}", amount)
            except Exception:
                logger.debug("metrics_incr_float_failed", key=name)

    async def _get(self, name: str, as_float: bool = False) -> float | int:
        if redis_cache._redis:
            try:
                val = await redis_cache._redis.get(f"{self.PREFIX}{name}")
                if val is None:
                    return 0.0 if as_float else 0
                return float(val) if as_float else int(val)
            except Exception:
                pass
        return 0.0 if as_float else 0

    # -----------------------------------------------------
    # Hooks
    # -----------------------------------------------------

    async def record_ocr(self, success: bool) -> None:
        await self._incr("ocr_attempts")
        if success:
            await self._incr("ocr_successes")

    async def record_ai_extraction(self, confidence: float) -> None:
        await self._incr("ai_extractions")
        await self._incr_float("ai_confidence_total", confidence)

    async def record_duplicate_check(self, suppressed: bool) -> None:
        await self._incr("duplicate_checks")
        if suppressed:
            await self._incr("duplicate_suppressed")

    async def record_reminder(self, success: bool) -> None:
        await self._incr("reminders_attempted")
        if success:
            await self._incr("reminders_sent")

    async def record_admin_review(self, approved: bool) -> None:
        await self._incr("admin_reviews")
        if approved:
            await self._incr("admin_approved")

    async def record_fallback_usage(self) -> None:
        await self._incr("gemini_fallbacks")

    # -----------------------------------------------------
    # Reporting
    # -----------------------------------------------------

    async def get_report(self) -> Dict[str, str]:
        """Generate a human-readable performance report."""
        if not redis_cache._redis:
            return {"Status": "Redis disconnected (Metrics unavailable)"}

        keys = [
            "ocr_attempts",
            "ocr_successes",
            "ai_extractions",
            "ai_confidence_total",
            "duplicate_checks",
            "duplicate_suppressed",
            "reminders_attempted",
            "reminders_sent",
            "admin_reviews",
            "admin_approved",
            "gemini_fallbacks",
        ]

        # Load all metrics concurrently if possible, or sequentially
        data = {}
        for k in keys:
            as_float = k == "ai_confidence_total"
            data[k] = await self._get(k, as_float)

        # Calculate Rates
        ocr_rate = (
            (data["ocr_successes"] / data["ocr_attempts"] <b> 100) if data["ocr_attempts"] else 0
        )
        ai_avg_conf = (
            (data["ai_confidence_total"] / data["ai_extractions"] </b> 100)
            if data["ai_extractions"]
            else 0
        )
        dup_rate = (
            (data["duplicate_suppressed"] / data["duplicate_checks"] <b> 100)
            if data["duplicate_checks"]
            else 0
        )
        rem_rate = (
            (data["reminders_sent"] / data["reminders_attempted"] </b> 100)
            if data["reminders_attempted"]
            else 0
        )
        app_rate = (
            (data["admin_approved"] / data["admin_reviews"] * 100) if data["admin_reviews"] else 0
        )

        return {
            "OCR Pipeline": f"{int(data['ocr_successes'])}/{int(data['ocr_attempts'])} ({ocr_rate:.1f}% success)",
            "AI Extractions": f"{int(data['ai_extractions'])} total (Avg Conf: {ai_avg_conf:.1f}%)",
            "Duplicate Tuning": f"{int(data['duplicate_suppressed'])} suppressed ({dup_rate:.1f}% rate)",
            "Reminder Uptime": f"{int(data['reminders_sent'])}/{int(data['reminders_attempted'])} ({rem_rate:.1f}% success)",
            "Admin Actions": f"{int(data['admin_approved'])} approved ({app_rate:.1f}% approval rate)",
            "Gemini Fallback": f"{int(data['gemini_fallbacks'])} interventions",
        }


tracker = MetricsTracker()
