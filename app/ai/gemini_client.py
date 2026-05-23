"""Gemini API client wrapper for fallback and advanced reasoning."""

import json
from typing import Any
import google.generativeai as genai

from app.config import settings
from app.logging import get_logger

logger = get_logger("gemini")

DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiClient:
    """Async Gemini client for fallback extraction."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_MODEL,
        client_name: str = "gemini",
    ) -> None:
        self._api_key = api_key if api_key is not None else settings.gemini_api_key
        self._default_model = default_model
        self._client_name = client_name
        self.is_configured = bool(self._api_key)
        if self.is_configured:
            genai.configure(api_key=self._api_key)
            # Create generative model with JSON output setting if supported
            # For gemini-1.5, response_mime_type="application/json" is valid
            self.model = genai.GenerativeModel(
                self._default_model, generation_config={"response_mime_type": "application/json"}
            )
        else:
            logger.warning("gemini_no_key", client=self._client_name)

    async def complete(
        self,
        system_instruction: str,
        user_prompt: str,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        """Send a completion to Gemini for fallback extraction."""
        if not self.is_configured:
            return {}

        import asyncio

        loop = asyncio.get_running_loop()

        def _generate() -> str:
            # We must pass the system instruction as part of the model if available,
            # but simpler approach: combine them for the prompt.
            # Using system_instruction arg supported in recent SDKs:
            model = genai.GenerativeModel(
                self._default_model,
                system_instruction=system_instruction,
                generation_config={"response_mime_type": "application/json"},
            )
            response = model.generate_content(user_prompt)
            return response.text

        for attempt in range(max_retries):
            try:
                content = await loop.run_in_executor(None, _generate)
                logger.info("gemini_success", client=self._client_name, model=self._default_model)
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    return {"raw": content}

            except Exception as e:
                logger.error("gemini_error", attempt=attempt, error=type(e).__name__)
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    continue

        return {}


# Module-level singleton
gemini_client = GeminiClient()
