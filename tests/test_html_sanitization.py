import pytest
from app.utils.text import sanitize_telegram_html

def test_sanitize_telegram_html_preserves_valid():
    """Valid Telegram HTML tags should not be escaped."""
    text = "<b>bold</b> <i>italic</i> <code>code</code> <a href=\"http://example.com\">link</a>"
    assert sanitize_telegram_html(text) == text

def test_sanitize_telegram_html_escapes_math():
    """Math symbols and unescaped brackets should be escaped."""
    text = "Here is math: x < y and z > y"
    assert sanitize_telegram_html(text) == "Here is math: x &lt; y and z &gt; y"

def test_sanitize_telegram_html_escapes_unsupported_tags():
    """Unsupported tags like <div> or <script> should be escaped."""
    text = "<div>Hello</div><script>alert(1)</script>"
    assert sanitize_telegram_html(text) == "&lt;div&gt;Hello&lt;/div&gt;&lt;script&gt;alert(1)&lt;/script&gt;"

def test_sanitize_telegram_html_mixed_content():
    """Mixed valid formatting and invalid tags/math."""
    text = "<b>Important:</b> if x < 5, then <math> fails!"
    assert sanitize_telegram_html(text) == "<b>Important:</b> if x &lt; 5, then &lt;math&gt; fails!"

def test_sanitize_telegram_html_malformed():
    """Should safely escape malformed HTML."""
    text = "<b>bold<"
    assert sanitize_telegram_html(text) == "<b>bold&lt;"

def test_sanitize_telegram_html_amharic_mixed():
    """Mixed Amharic and brackets."""
    text = "<b>ማስታወቂያ</b>: <math_equation> x < y"
    assert sanitize_telegram_html(text) == "<b>ማስታወቂያ</b>: &lt;math_equation&gt; x &lt; y"
