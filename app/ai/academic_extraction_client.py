"""Dedicated AI client for Academic OS extraction."""

from typing import Any

from app.ai.gemini_client import GeminiClient
from app.ai.groq_client import GroqClient
from app.config import settings
from app.logging import get_logger

logger = get_logger("academic_extraction_ai")


class AcademicExtractionClient:
    """Separate quota/rate-limit boundary for academic extraction."""

    def __init__(self) -> None:
        self._groq = GroqClient(
            api_key=settings.academic_groq_api_key or settings.groq_api_key,
            requests_per_minute=settings.academic_ai_requests_per_minute,
            default_model=settings.academic_extraction_model,
            client_name="academic_extraction",
        )
        self._gemini = GeminiClient(
            api_key=settings.academic_gemini_api_key or settings.gemini_api_key,
            client_name="academic_extraction_fallback",
        )

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict | None = None,
    ) -> dict[str, Any]:
        """Complete an extraction request with the academic model boundary."""
        result = await self._groq.complete(
            messages,
            response_format=response_format,
            temperature=0.0,
            max_tokens=800,
        )
        if result:
            return result

        if self._gemini.is_configured:
            logger.info("academic_extraction_fallback_to_gemini")
            return await self._gemini.complete(messages[0]["content"], messages[1]["content"])

        return {}


academic_extraction_client = AcademicExtractionClient()
