"""Image OCR processing pipeline."""

import base64
import json
from typing import Optional

from app.logging import get_logger

logger = get_logger("ocr_engine")

async def extract_text_from_image(image_bytes: bytes) -> Optional[str]:
    """Execute OCR on an image using Groq Llama 3.2 Vision. Returns extracted text or None on failure."""
    from app.ai.groq_client import groq_client
    from app.metrics.tracker import tracker
    
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract all text exactly as written from this image. Do not hallucinate or add any chatty introductory text. If there is Amharic script, transcribe it accurately. Just output the raw text found in the image."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        }
                    }
                ]
            }
        ]
        
        result = await groq_client.complete(
            messages,
            model="llama-3.2-11b-vision-preview",
            temperature=0.0,
            max_tokens=2048,
        )
        
        text = ""
        if isinstance(result, dict):
            text = result.get("raw", "")
            if not text and result:
                # In case the OCR extracted valid JSON and the client parsed it
                text = json.dumps(result, ensure_ascii=False)
                
        if text and text.strip():
            await tracker.record_ocr(success=True)
            logger.info("ocr_success", extracted_chars=len(text))
            return text.strip()
            
        await tracker.record_ocr(success=False)
        return None
    except Exception as e:
        logger.exception("ocr_failed", error=type(e).__name__)
        try:
            from app.metrics.tracker import tracker
            await tracker.record_ocr(success=False)
        except Exception:
            pass
        return None

