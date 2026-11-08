"""Build short-lived, shareable visual study pages for Telegram lessons."""

import html
import json
import re
import secrets
from urllib.parse import urlparse

import course_tutor_ai

LESSON_TTL_MS = 30 * 24 * 60 * 60 * 1000
MAX_LESSON_ID_CHARS = 48
MAX_ARTICLE_CHARS = 16_000
MAX_SOURCES = 3


def _clean_title(value: str) -> str:
    value = re.sub(r"[`*_#]", "", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" .:-")
    return value[:180] or "Study explanation"


def _title_from_answer(answer: str) -> str:
    for line in str(answer or "").splitlines():
        match = re.match(r"\s*#{1,6}\s+(.+?)\s*$", line)
        if not match:
            match = re.match(r"\s*\*\*(.{3,100}?)\*\*\s*$", line)
        if match:
            candidate = _clean_title(match.group(1))
            if candidate.casefold() not in {"answer", "explanation", "summary", "key idea"}:
                return candidate
    first = re.sub(r"\s+", " ", str(answer or "").strip()).split(". ", 1)[0]
    return _clean_title(first[:100])


def _safe_sources(web_sources: list[dict]) -> list[dict]:
    result = []
    for source in web_sources[:MAX_SOURCES]:
        if not isinstance(source, dict):
            continue
        url = source.get("url")
        if not isinstance(url, str):
            continue
        if not course_tutor_ai._safe_public_https_url(url):
            continue
        parsed = urlparse(url.strip())
        safe_images = []
        for image in source.get("images", []) if isinstance(source.get("images"), list) else []:
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                continue
            image_url = image["url"].strip()
            if not course_tutor_ai._safe_public_https_url(image_url):
                continue
            safe_images.append({
                "url": image_url[:500],
                "description": str(image.get("description") or "")[:180],
            })
            if len(safe_images) == 1:
                break
        result.append({
            "title": _clean_title(source.get("title") or "Reference")[:140],
            "url": url.strip()[:500],
            "domain": parsed.netloc[:140],
            "images": safe_images,
        })
    return result


def _lesson_article_html(answer: str, sources: list[dict], course_material_used: bool) -> str:
    citations = {}
    for index, source in enumerate(sources, 1):
        link = html.escape(source["url"], quote=True)
        label = html.escape(source["title"][:48], quote=False)
        citations[f"W{index}"] = f'<a href="{link}">{label}</a>'
    rendered = course_tutor_ai.markdown_to_telegram_html(
        str(answer or "")[:9_000], citation_links=citations
    )
    body_parts = []
    for part in re.split(r"(<pre>.*?</pre>|<blockquote>.*?</blockquote>)", rendered, flags=re.DOTALL):
        if not part:
            continue
        if part.startswith("<pre>") or part.startswith("<blockquote>"):
            body_parts.append(part)
            continue
        for paragraph in re.split(r"\n\s*\n", part):
            lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
            if not lines:
                continue
            if all(line.startswith("• ") for line in lines):
                body_parts.append("<ul>" + "".join(f"<li>{line[2:]}</li>" for line in lines) + "</ul>")
            elif len(lines) == 1 and re.fullmatch(r"<b>.*</b>", lines[0], re.DOTALL):
                body_parts.append(f"<h2>{lines[0][3:-4]}</h2>")
            else:
                body_parts.append("<p>" + "<br>".join(lines) + "</p>")
    body = "\n".join(body_parts)
    if course_material_used:
        body += (
            '<p class="privacy-note">Course materials informed this explanation. '
            "Private class posts and course file links are not included on this page.</p>"
        )
    return body


def _visual_steps(answer: str) -> list[tuple[str, str]]:
    lines = str(answer or "").splitlines()
    headings: list[tuple[str, str]] = []
    current_heading = None
    current_detail = []
    in_code = False

    def save_heading():
        if current_heading:
            detail = re.sub(r"\s+", " ", " ".join(current_detail)).strip()
            detail = re.sub(r"\[(?:M|W)\d+\]", "", detail)
            headings.append((_clean_title(current_heading)[:42], detail[:105]))

    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        match = re.match(r"\s*#{1,6}\s+(.+?)\s*$", line)
        if not match:
            match = re.match(r"\s*\*\*(.{3,80}?)\*\*\s*$", line)
        if not match:
            match = re.match(r"\s*\d+[.)]\s+(.{3,80})$", line)
        if match:
            save_heading()
            current_heading = match.group(1)
            current_detail = []
        elif current_heading and line.strip():
            current_detail.append(re.sub(r"[*_`#]", "", line.strip()))
    save_heading()

    steps = []
    for title, detail in headings:
        if title.casefold() not in {"additional sources", "sources", "course materials", "takeaway"}:
            steps.append((title, detail or "Explore this part of the explanation."))
        if len(steps) == 3:
            break
    if len(steps) < 3:
        sentences = [
            re.sub(r"\s+", " ", item).strip()
            for item in re.split(r"(?<=[.!?])\s+", re.sub(r"[#*_`]", "", answer))
            if item.strip()
        ]
        defaults = ["Core idea", "Worked example", "Apply the idea"]
        steps = [
            (steps[i][0], steps[i][1]) if i < len(steps)
            else (defaults[i], sentences[i][:105] if i < len(sentences) else "A short step to build understanding.")
            for i in range(3)
        ]
    return steps


def build_visual_svg(answer: str) -> str:
    """Render a compact lesson pathway from the tutor's own section headings."""
    steps = _visual_steps(answer)
    cards = []
    for index, (title, detail) in enumerate(steps):
        x = 24 + index * 306
        safe_title = html.escape(title[:25])
        safe_detail = html.escape(detail[:92])
        words = safe_detail.split()
        first, second = "", ""
        for word in words:
            if len(first) + len(word) < 34:
                first = (first + " " + word).strip()
            elif len(second) + len(word) < 34:
                second = (second + " " + word).strip()
            else:
                break
        cards.append(
            f'<g><rect x="{x}" y="44" width="282" height="252" rx="24" fill="#ffffff" stroke="#dbe5f2"/>'
            f'<circle cx="{x + 34}" cy="82" r="17" fill="#e9f1ff"/>'
            f'<text x="{x + 34}" y="88" text-anchor="middle" font-size="16" font-weight="700" fill="#315fbd">{index + 1}</text>'
            f'<text x="{x + 24}" y="137" font-size="21" font-weight="700" fill="#18253d">{safe_title}</text>'
            f'<text x="{x + 24}" y="180" font-size="15" fill="#53637a">{first}</text>'
            f'<text x="{x + 24}" y="204" font-size="15" fill="#53637a">{second}</text>'
            f'<path d="M{x + 24} 246h58" stroke="#8eb8ff" stroke-width="4" stroke-linecap="round"/></g>'
        )
    connectors = ''.join(
        f'<path d="M{24 + i * 306 + 282} 170h24" stroke="#7b9cda" stroke-width="3" stroke-linecap="round"/>'
        f'<path d="m{24 + i * 306 + 298} 164 6 6-6 6" fill="none" stroke="#7b9cda" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
        for i in range(2)
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 340" role="img" '
        'aria-labelledby="visual-title visual-desc">'
        '<title id="visual-title">Visual lesson pathway</title>'
        '<desc id="visual-desc">Three steps that organize the explanation from its key idea through an example.</desc>'
        '<rect width="960" height="340" rx="30" fill="#f3f7ff"/>'
        '<text x="30" y="28" font-size="13" font-weight="700" letter-spacing="1.6" fill="#61738e">VISUAL WALKTHROUGH</text>'
        + ''.join(cards) + connectors + '</svg>'
    )


def render_lesson_page(row: dict, base_url: str) -> str:
    title = html.escape(str(row["title"]))
    article = str(row["article_html"])
    sources = json.loads(row["sources_json"] or "[]")
    visual_items = []
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, dict):
            continue
        for image in source.get("images", []) if isinstance(source.get("images"), list) else []:
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                continue
            image_url = image["url"].strip()
            if not course_tutor_ai._safe_public_https_url(image_url):
                continue
            caption = html.escape(str(image.get("description") or source.get("title") or "Related study visual")[:180])
            source_title = html.escape(str(source.get("title") or "Reference")[:100])
            image_src = html.escape(image_url, quote=True)
            source_url = html.escape(str(source.get("url") or ""), quote=True)
            visual_items.append(
                f'<figure><img src="{image_src}" alt="{caption}" loading="lazy" referrerpolicy="no-referrer">'
                f'<figcaption>{caption}<br><a href="{source_url}" rel="noopener noreferrer">Image context: {source_title}</a></figcaption></figure>'
            )
    image_gallery = (
        '<section class="web-visuals"><h2>Visual examples</h2><div class="gallery">' + ''.join(visual_items) + '</div></section>'
        if visual_items else ""
    )
    source_items = "".join(
        '<li><a href="' + html.escape(source["url"], quote=True)
        + '" rel="noopener noreferrer"><span>' + html.escape(source["title"])
        + '</span><small>' + html.escape(source["domain"]) + '</small></a></li>'
        for source in sources if isinstance(source, dict)
    )
    sources_section = (
        '<section class="sources"><h2>References</h2><ul>' + source_items + '</ul></section>'
        if source_items else ""
    )
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><meta name="theme-color" content="#f3f7ff">
<meta property="og:type" content="article"><meta property="og:title" content="{title}">
<meta property="og:description" content="A clear study explanation with a visual example.">
<title>{title} · Study lesson</title>
<style>
:root{{color-scheme:light dark;--ink:#18253d;--muted:#617089;--paper:#fff;--wash:#f3f7ff;--line:#e2e9f3;--accent:#315fbd}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--wash);color:var(--ink);font:17px/1.72 system-ui,-apple-system,"Segoe UI",sans-serif}}
.shell{{max-width:820px;margin:0 auto;padding:clamp(20px,5vw,54px) 20px 48px}}
.eyebrow{{color:var(--accent);font-size:12px;font-weight:750;letter-spacing:.14em;text-transform:uppercase}}
h1{{font-size:clamp(32px,7vw,52px);letter-spacing:-.04em;line-height:1.12;margin:12px 0 14px}}
.intro{{color:var(--muted);font-size:18px;margin:0 0 28px}}
.visual{{margin:28px -8px 34px}}.visual img{{display:block;width:100%;height:auto;border-radius:22px}}
.web-visuals{{margin:32px 0}}.web-visuals h2{{font-size:21px;letter-spacing:-.02em}}.gallery{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:16px}}.gallery figure{{overflow:hidden;margin:0;background:var(--paper);border:1px solid var(--line);border-radius:18px;box-shadow:0 10px 28px #1e36510b}}.gallery img{{display:block;width:100%;aspect-ratio:16/10;object-fit:cover;background:var(--wash)}}.gallery figcaption{{padding:12px 14px;color:var(--muted);font-size:13px;line-height:1.5}}.gallery a{{color:var(--accent)}}
.article{{background:var(--paper);border:1px solid var(--line);border-radius:22px;padding:clamp(22px,5vw,42px);box-shadow:0 12px 36px #1e36510b}}
.article p{{margin:0 0 1.1em}}.article h2{{font-size:1.25em;line-height:1.3;margin:1.7em 0 .65em;letter-spacing:-.02em}}.article b{{font-weight:720}}.article a{{color:var(--accent);text-decoration-thickness:1px;text-underline-offset:3px}}
.article ul{{padding-left:1.25em;margin:.4em 0 1.2em}}.article li{{padding-left:.25em;margin:.35em 0}}
.article pre{{overflow:auto;background:#101a2b;color:#e8efff;border-radius:14px;padding:18px;font-size:.9em;line-height:1.55}}
.article code{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}}.article :not(pre)>code{{background:#edf2fa;border-radius:5px;padding:.12em .36em;color:#24385b}}
.article blockquote{{border-left:3px solid #8eb8ff;margin:1em 0;padding:.1em 0 .1em 1em;color:var(--muted)}}
.sources{{margin-top:28px}}.sources h2{{font-size:16px}}.sources ul{{list-style:none;padding:0;display:grid;gap:10px}}
.sources a{{display:flex;justify-content:space-between;gap:16px;padding:13px 16px;background:var(--paper);border:1px solid var(--line);border-radius:13px;color:var(--ink);text-decoration:none}}
.sources small{{color:var(--muted)}}.privacy-note{{margin-top:25px;padding:14px 16px;border-radius:12px;background:var(--wash);color:var(--muted);font-size:14px}}
.footer{{margin:24px 4px 0;color:var(--muted);font-size:13px}}
@media(prefers-color-scheme:dark){{:root{{--ink:#e8edf7;--muted:#a8b3c7;--paper:#151e2c;--wash:#0e1520;--line:#29364a;--accent:#91b7ff}}.article :not(pre)>code{{background:#27364b;color:#e8efff}}}}
</style></head><body><main class="shell"><header><div class="eyebrow">Study lesson</div><h1>{title}</h1>
<p class="intro">A clear explanation, organized around a visual walkthrough.</p></header>
<figure class="visual"><img src="{html.escape(base_url.rstrip('/'), quote=True)}/lesson/{html.escape(str(row['lesson_id']), quote=True)}/visual.svg" alt="Three-step visual summary of the lesson"></figure>
{image_gallery}
<article class="article">{article}</article>{sources_section}
<p class="footer">Shareable lesson · Private chat history and course post links are not shown here.</p></main></body></html>'''


def contains_personal_details(question: str, answer: str) -> bool:
    content = f"{question}\n{answer}"
    return bool(re.search(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
        r"\b(?:student\s*(?:id|number)|matric(?:ulation)?\s*(?:id|number))\s*[:#-]?\s*[A-Z0-9-]{4,}\b|"
        r"(?<!\w)@\w{3,}",
        content,
        re.IGNORECASE,
    ))


async def create_visual_lesson(
    database,
    question: str,
    answer: str,
    web_sources: list[dict],
    now_ms: int,
    base_url: str,
    course_material_used: bool = False,
) -> str | None:
    """Persist a redacted, source-linked explanation without chat or course-post IDs."""
    if contains_personal_details(question, answer):
        return None
    lesson_id = secrets.token_urlsafe(24)
    title = _title_from_answer(answer)
    sources = _safe_sources(web_sources)
    article = _lesson_article_html(answer, sources, course_material_used)
    svg = build_visual_svg(answer)
    if len(article) > MAX_ARTICLE_CHARS or len(svg) > 16_000:
        return None
    expires_at_ms = now_ms + LESSON_TTL_MS
    await database.prepare(
        """INSERT INTO tutor_visual_lessons (
            lesson_id, title, article_html, visual_svg, sources_json, created_at_ms, expires_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?)"""
    ).bind(
        lesson_id,
        title,
        article,
        svg,
        json.dumps(sources, ensure_ascii=False),
        now_ms,
        expires_at_ms,
    ).run()
    return f"{base_url.rstrip('/')}/lesson/{lesson_id}"
