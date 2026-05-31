"""Tests for the image OCR pipeline."""

import pytest
import io
from unittest.mock import patch
from PIL import Image

from app.ocr.engine import extract_text_from_image


@pytest.mark.asyncio
async def test_extract_text_from_image_success():
    """Test successful OCR extraction."""
    with patch(
        "app.ai.groq_client.GroqClient.complete", return_value={"raw": "Dummy assignment due tomorrow"}
    ):
        # Create a valid 1x1 JPEG in memory
        img = Image.new("RGB", (1, 1))
        b = io.BytesIO()
        img.save(b, format="JPEG")

        text = await extract_text_from_image(b.getvalue())
        assert text == "Dummy assignment due tomorrow"


@pytest.mark.asyncio
async def test_extract_text_from_image_failure():
    """Test OCR failure handling (e.g. invalid bytes or missing tesseract)."""
    with patch(
        "app.ai.groq_client.GroqClient.complete", side_effect=Exception("API crash")
    ):
        img = Image.new("RGB", (1, 1))
        b = io.BytesIO()
        img.save(b, format="JPEG")

        text = await extract_text_from_image(b.getvalue())
        assert text is None


@pytest.mark.asyncio
async def test_extract_text_invalid_bytes():
    """Test OCR with completely invalid byte data (fails at API)."""
    with patch(
        "app.ai.groq_client.GroqClient.complete", side_effect=Exception("API rejection for bad bytes")
    ):
        text = await extract_text_from_image(b"not an image")
        assert text is None
