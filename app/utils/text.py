"""Text normalization and Telegram formatting utilities."""

import re

# ---------------------------------------------------------------------------
# Telegram MarkdownV2 escaping
# ---------------------------------------------------------------------------

_MARKDOWNV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _MARKDOWNV2_SPECIAL.sub(r"\\\1", text)


# ---------------------------------------------------------------------------
# Basic language detection / normalization
# ---------------------------------------------------------------------------

_AMHARIC_RANGE = re.compile(r"[\u1200-\u137F]")


def contains_amharic(text: str) -> bool:
    """Check if text contains Amharic script characters."""
    return bool(_AMHARIC_RANGE.search(text))


def normalize_whitespace(text: str) -> str:
    """Collapse multiple whitespace into single spaces and strip."""
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Keyword normalization for common Amharic-English hybrid patterns
# ---------------------------------------------------------------------------

_KEYWORD_NORMALIZATIONS: dict[str, str] = {
    "nw": "is",
    "be": "by",
    "yemigeba": "deadline",
    "yemigebaw": "deadline",
    "fetena": "exam",
    "yefetena": "exam",
    "sira": "assignment",
    "yebet sira": "homework",
    "yebetsira": "homework",
    "mekera": "quiz",
    "timhirt": "course",
    "kefl": "chapter",
    "mermera": "final",
    "mekakeya": "mid",
}


def normalize_keywords(text: str) -> str:
    """Replace common Amharic-transliterated keywords with English equivalents."""
    words = text.lower().split()
    normalized = []
    for word in words:
        normalized.append(_KEYWORD_NORMALIZATIONS.get(word, word))
    return " ".join(normalized)


def clean_text(text: str) -> str:
    """Full text cleanup pipeline: whitespace + keyword normalization."""
    text = normalize_whitespace(text)
    text = normalize_keywords(text)
    return text
