"""Groq API client wrapper with retry, rate limiting, and error handling."""

import asyncio
import json
import time
from typing import Any

import httpx

from app.config import settings
from app.logging import get_logger

logger = get_logger("groq")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"


class GroqClient:
    """Async Groq API client with retry and rate limiting."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        requests_per_minute: int | None = None,
        default_model: str = DEFAULT_MODEL,
        client_name: str = "groq",
    ) -> None:
        self._client = httpx.AsyncClient(timeout=30.0)
        self._api_key = api_key if api_key is not None else settings.groq_api_key
        self._default_model = default_model
        self._client_name = client_name
        self._last_request_time = 0.0
        rpm = requests_per_minute or settings.ai_requests_per_minute
        self._min_interval = 60.0 / max(rpm, 1)

    async def close(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        response_format: dict | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """Send a chat completion request to Groq.

        Returns:
            Parsed JSON response content.
        """
        if not self._api_key:
            logger.warning("groq_no_key", client=self._client_name)
            return {}

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        for attempt in range(max_retries):
            await self._rate_limit()

            try:
                response = await self._client.post(GROQ_API_URL, json=payload, headers=headers)

                if response.status_code == 200:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                    logger.info(
                        "groq_success",
                        client=self._client_name,
                        model=model or self._default_model,
                        tokens=data.get("usage", {}).get("total_tokens"),
                    )
                    # Try to parse as JSON
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError:
                        return {"raw": content}

                elif response.status_code == 429:
                    wait = 2**attempt
                    logger.warning("groq_rate_limited", retry_in=wait)
                    await asyncio.sleep(wait)
                    continue

                else:
                    logger.error(
                        "groq_error",
                        status=response.status_code,
                        body=response.text[:200],
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                        continue
                    return {}

            except httpx.TimeoutException:
                logger.warning("groq_timeout", attempt=attempt)
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                return {}

            except Exception:
                logger.exception("groq_unexpected_error", attempt=attempt)
                return {}

        return {}

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        filename: str = "audio.ogg",
        model: str = "whisper-large-v3",
        max_retries: int = 3,
    ) -> str | None:
        """Transcribe audio using Groq's Whisper API."""
        if not self._api_key:
            return None

        headers = {
            "Authorization": f"Bearer {self._api_key}",
        }

        files = {
            "file": (filename, audio_bytes, "audio/ogg"),
            "model": (None, model),
            "response_format": (None, "json"),
            "prompt": (None, "Ethiopian academic context. Language could be Amharic or English."),
        }

        for attempt in range(max_retries):
            await self._rate_limit()

            try:
                response = await self._client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    files=files,
                    headers=headers,
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info("groq_transcribe_success", model=model)
                    return data.get("text")

                elif response.status_code == 429:
                    wait = 2**attempt
                    await asyncio.sleep(wait)
                    continue

                else:
                    logger.error(
                        "groq_transcribe_error",
                        status=response.status_code,
                        body=response.text[:200],
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                        continue
                    return None

            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                return None

        return None

    async def _rate_limit(self) -> None:
        """Simple rate limiter based on minimum interval between requests."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_time = time.monotonic()


# Module-level singleton
groq_client = GroqClient()
