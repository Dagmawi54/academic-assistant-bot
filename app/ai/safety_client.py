"""Safety/Moderation API Client using Gemini Vision."""

import asyncio
from typing import Optional

import google.generativeai as genai

from app.config import settings
from app.logging import get_logger

logger = get_logger("safety_client")


class SafetyClient:
    """Uses Gemini 2.5 Flash as an automated visual moderation layer."""

    def __init__(self) -> None:
        self.api_key = settings.gemini_api_key
        self.is_configured = bool(self.api_key)
        if self.is_configured:
            genai.configure(api_key=self.api_key)

    async def is_safe(self, media_bytes: bytes, mime_type: str = "image/jpeg") -> bool:
        """
        Check if an image or WebP is safe for an academic environment.
        Returns False if the image contains hate speech, extreme toxicity, sexually explicit content, or dangerous behavior.
        Returns True if safe or if the API cannot be reached.
        """
        if not self.is_configured:
            return True

        prompt = (
            "Analyze the provided image/sticker/gif. "
            "Respond with precisely one word: 'SAFE' or 'UNSAFE'. "
            "Consider it 'UNSAFE' if it contains sexually explicit material, graphic violence, "
            "hate speech, severe profanity, or harassment. If it is just a meme, schedule, or normal photo, respond 'SAFE'."
        )

        try:
            loop = asyncio.get_running_loop()

            def _check() -> str | None:
                # We use the fast generic model which supports multimodal input
                model = genai.GenerativeModel("gemini-2.5-flash")
                response = model.generate_content(
                    [
                        prompt,
                        {"mime_type": mime_type, "data": media_bytes},
                    ]
                )
                return response.text

            # Execute blocking API call in thread pool
            text_response = await loop.run_in_executor(None, _check)

            if text_response and "UNSAFE" in text_response.upper():
                logger.warning("safety_check_failed", response=text_response.strip())
                return False

            return True

        except Exception as e:
            logger.exception("safety_client_error", error=type(e).__name__)
            # Fail open: don't delete on API crash
            return True


safety_client = SafetyClient()
