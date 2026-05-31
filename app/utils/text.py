"""Text normalization and Telegram formatting utilities."""

import re

# ---------------------------------------------------------------------------
# Telegram MarkdownV2 escaping
# ---------------------------------------------------------------------------

_MARKDOWNV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+-=|{}.!\\])")


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


def sanitize_telegram_html(text: str) -> str:
    """Safely escape text for Telegram HTML parse_mode while preserving supported formatting.
    
    Telegram only supports a strict subset of HTML: <b>, <i>, <u>, <s>, <code>, <pre>, <a>, etc.
    Any other unescaped '<' or '>' (like in math equations or code snippets) will crash the bot.
    """
    import html
    
    # Allowed tags by Telegram
    allowed_tags = {
        "b", "/b", "strong", "/strong",
        "i", "/i", "em", "/em",
        "u", "/u", "ins", "/ins",
        "s", "/s", "strike", "/strike", "del", "/del",
        "code", "/code", "pre", "/pre",
        "blockquote", "/blockquote",
    }
    
    # Regex to find potential HTML tags
    # Matches <something> or </something> or <a href="...">
    pattern = re.compile(r"<(/?[a-zA-Z0-9\-]+)([^>]*)>")
    
    def replacer(match: re.Match) -> str:
        tag_name = match.group(1).lower()
        
        # If it's a valid Telegram tag (or an anchor tag), keep it intact
        if tag_name in allowed_tags or (tag_name == "a" and "href=" in match.group(2).lower()) or tag_name == "/a":
            return match.group(0)
            
        # Otherwise, escape it (e.g. <math> becomes &lt;math&gt;)
        return html.escape(match.group(0))

    # We must escape standalone < and > that don't match the tag schema above.
    # To do this safely, we FIRST find all valid tags, temporarily swap them, escape the rest, and swap back.
    
    # 1. Temporarily replace valid tags with placeholders
    placeholders = []
    def preserve_valid(match: re.Match) -> str:
        full_tag = match.group(0)
        tag_name = match.group(1).lower()
        if tag_name in allowed_tags or (tag_name == "a" and "href=" in match.group(2).lower()) or tag_name == "/a":
            placeholders.append(full_tag)
            return f"@@TAG_{len(placeholders)-1}@@"
        return full_tag

    text_with_placeholders = pattern.sub(preserve_valid, text)
    
    # 2. Escape the remaining string completely (catches bare <, >, &)
    escaped_text = html.escape(text_with_placeholders)
    
    # 3. Restore the valid tags
    for i, p in enumerate(placeholders):
        escaped_text = escaped_text.replace(f"@@TAG_{i}@@", p)
        
    return escaped_text



class ResponseFormatter:
    @staticmethod
    def normalize(text: str) -> str:
        """
        Normalizes outputs before sending to Telegram UI.
        - Strips DEBUG/TRACE leaks.
        - Clamps excessive newlines to max 2.
        - Prevents raw stack dumps.
        - Converts unsafe markdown (**, __) to HTML safe tags if requested, then uses sanitize.
        """
        # Collapse excessive newlines
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        # Remove debug leaks
        lines = text.split("\n")
        safe_lines = []
        for line in lines:
            trimmed = line.strip()
            if trimmed.startswith("DEBUG:") or trimmed.startswith("TRACE:"):
                continue
            if "Traceback (most recent call last):" in trimmed:
                break
            safe_lines.append(line)
        text = "\n".join(safe_lines)

        # Convert simple markdown to HTML tags if models slip up
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'__(.+?)__', r'<i>\1</i>', text)
        
        # Format headers as bold
        text = re.sub(r'(?m)^###?\s+(.+)$', r'<b>\1</b>', text)
        # Format lists
        text = re.sub(r'(?m)^[\*\-]\s+', r'• ', text)
        
        # Remove standalone hash markup
        text = re.sub(r'(?m)^#\s+(.+)$', r'<b>\1</b>', text)

def clean_text(text: str) -> str:
    """Full text cleanup pipeline: whitespace + keyword normalization."""
    text = normalize_whitespace(text)
    text = normalize_keywords(text)
    return text
