"""Document parsing engine for PDFs and DOCX files."""

import io
import asyncio
from typing import Optional

import pdfplumber
import docx

from app.logging import get_logger

logger = get_logger("file_parser")


async def extract_text_from_pdf(pdf_bytes: bytes) -> Optional[str]:
    """Extract text from a PDF file safely in a thread."""
    loop = asyncio.get_running_loop()

    def _process() -> str:
        text_pages = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            # We only extract up to 10 pages to avoid overload on huge textbook PDFs
            for page in pdf.pages[:10]:
                text = page.extract_text()
                if text:
                    text_pages.append(text)
        return "\n".join(text_pages).strip()

    try:
        text = await loop.run_in_executor(None, _process)
        if text:
            logger.info("pdf_parsed", extracted_chars=len(text))
            return text
        return None
    except Exception as e:
        logger.exception("pdf_parse_failed", error=type(e).__name__)
        return None


async def extract_text_from_docx(docx_bytes: bytes) -> Optional[str]:
    """Extract text from a DOCX file safely in a thread."""
    loop = asyncio.get_running_loop()

    def _process() -> str:
        doc = docx.Document(io.BytesIO(docx_bytes))
        paragraphs = [para.text for para in doc.paragraphs if para.text]
        return "\n".join(paragraphs).strip()

    try:
        text = await loop.run_in_executor(None, _process)
        if text:
            logger.info("docx_parsed", extracted_chars=len(text))
            return text
        return None
    except Exception as e:
        logger.exception("docx_parse_failed", error=type(e).__name__)
        return None
