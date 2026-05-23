"""Dedicated AI client for /ask chatbot requests."""

from typing import Any

from app.ai.gemini_client import GeminiClient
from app.ai.groq_client import GroqClient
from app.config import settings
from app.logging import get_logger

logger = get_logger("chatbot_ai")


class ChatbotClient:
    """Separate quota/rate-limit boundary for the secondary /ask assistant."""

    def __init__(self) -> None:
        self._groq = GroqClient(
            api_key=settings.chatbot_groq_api_key or settings.groq_api_key,
            requests_per_minute=settings.chatbot_ai_requests_per_minute,
            default_model=settings.chatbot_model,
            client_name="chatbot",
        )
        self._gemini = GeminiClient(
            api_key=settings.chatbot_gemini_api_key or settings.gemini_api_key,
            client_name="chatbot_fallback",
        )

    async def complete(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """Complete a chatbot request without sharing Academic OS quota."""
        result = await self._groq.complete(messages)
        if result:
            return result

        if self._gemini.is_configured:
            logger.info("chatbot_fallback_to_gemini")
            return await self._gemini.complete(messages[0]["content"], messages[1]["content"])

        return {}


chatbot_client = ChatbotClient()
