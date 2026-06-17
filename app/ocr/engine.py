"""Image OCR processing pipeline."""

import base64
import json
from typing import Optional

from app.logging import get_logger

logger = get_logger("ocr_engine")

async def extract_text_from_image(image_bytes: bytes) -> Optional[str]:
    """Execute OCR on an image using Groq Llama 3.2 Vision or Gemini. Returns extracted text or None."""
    from app.ai.groq_client import groq_client
    from app.metrics.tracker import tracker
    
    # 1. Resize image to prevent massive Base64 payloads kicking back HTTP 400 from Groq
    import io
    from PIL import Image
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            # Convert to RGB to ensure jpeg support
            if img.mode != 'RGB':
                img = img.convert('RGB')
            # 1024 max dimension to speed up OCR and reduce payload size
            img.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            optimized_bytes = buf.getvalue()
    except Exception as e:
        logger.error("image_resize_failed", error=type(e).__name__)
        optimized_bytes = image_bytes
        
    try:
        base64_image = base64.b64encode(optimized_bytes).decode('utf-8')
        
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
                text = json.dumps(result, ensure_ascii=False)
                
        # If Groq returns empty or completely fails, fallback to Gemini
        if not text or not text.strip():
            logger.info("groq_ocr_failed_fallback_to_gemini")
            from app.ai.gemini_client import gemini_client
            if gemini_client.is_configured:
                import google.generativeai as genai
                model = genai.GenerativeModel("gemini-2.5-flash")
                # Gemini takes PIL Image directly
                with Image.open(io.BytesIO(optimized_bytes)) as img:
                    import asyncio
                    loop = asyncio.get_running_loop()
                    def _generate():
                        response = model.generate_content([
                            "Extract all text exactly as written from this image. Output only the raw text.",
                            img
                        ])
                        return response.text
                    text = await loop.run_in_executor(None, _generate)

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

