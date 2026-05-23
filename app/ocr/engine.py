"""Image OCR processing pipeline."""

import io
import asyncio
from typing import Optional

from PIL import Image
import pytesseract

from app.logging import get_logger

logger = get_logger("ocr_engine")


async def extract_text_from_image(image_bytes: bytes) -> Optional[str]:
    """Execute OCR on an image. Returns extracted text or None on failure.
    Runs synchronously blocking pytesseract in an async executor thread.
    """
    loop = asyncio.get_running_loop()

    def _process() -> str:
        with Image.open(io.BytesIO(image_bytes)) as img:
            # Basic preprocessing: convert to grayscale
            img = img.convert("L")
            text = pytesseract.image_to_string(img)
            return text.strip()

    try:
        from app.metrics.tracker import tracker

        text = await loop.run_in_executor(None, _process)
        if text:
            await tracker.record_ocr(success=True)
            logger.info("ocr_success", extracted_chars=len(text))
            return text
        return None
    except Exception as e:
        from app.metrics.tracker import tracker
        await tracker.record_ocr(success=False)
        logger.exception("ocr_failed", error=type(e).__name__)
        return None
