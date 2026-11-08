"""Context-aware buttons: follow-up questions written from the answer itself, plus Continue only when cut off."""

import json
import logging
import re
import secrets

import course_tutor_ai

logger = logging.getLogger("academic_assistant_followups")

MAX_SUGGESTIONS = 3
LABEL_CHARS = 10
QUESTION_CHARS = 200
TTL_MS = 24 * 3600 * 1000
CONTINUE_PROMPT = (
    "Continue exactly where your previous answer stopped. Do not repeat anything already said, "
    "do not introduce the topic again; finish the unfinished part."
)


def plain(html_text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(html_text or ""))
    return re.sub(r"\s+", " ", text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")).strip()


def parse_suggestions(raw: str) -> list[dict]:
    items = []
    for line in str(raw or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if "|" not in line:
            continue
        label, question = (part.strip(" *`\"") for part in line.split("|", 1))
        if 3 <= len(label) and 8 <= len(question):
            items.append({"label": _button_label(label, question), "question": question[:QUESTION_CHARS]})
        if len(items) == MAX_SUGGESTIONS:
            break
    return items


def _button_label(label: str, question: str) -> str:
    """Use short, readable actions; keep the full contextual question behind the button."""
    normalized = f"{label} {question}".casefold()
    if re.search(r"\b(?:why|cause|reason|how does|how do)\b", normalized):
        return "Why?"
    if re.search(r"\b(?:example|examples|for instance|sample|demonstrate)\b", normalized):
        return "Example"
    if re.search(r"\b(?:apply|use this|try|work through)\b", normalized):
        return "Apply"
    if re.search(r"\b(?:compare|difference|versus|vs)\b", normalized):
        return "Compare"
    if re.search(r"\b(?:check|understand|mistake|quiz|test yourself)\b", normalized):
        return "Check"
    if re.search(r"\bwhat if\b", normalized):
        return "What if?"
    return "Explore"


def _relevant_suggestions(items: list[dict], question: str, answer: str) -> list[dict]:
    """Drop follow-ups that introduce a subject absent from both the question and its answer."""
    context = set(course_tutor_ai._content_terms(f"{question} {answer[:1600]}"))
    relevant = []
    for item in items:
        terms = set(course_tutor_ai._content_terms(item.get("question", "")))
        shared = context.intersection(terms)
        # One generic word such as “model” or “law” is not enough to establish
        # that a suggested button belongs to this answer. Omit weak suggestions.
        if shared and (len(shared) >= 2 or any(len(term) >= 6 for term in shared)):
            relevant.append(item)
    return relevant[:MAX_SUGGESTIONS]


async def suggest(ai_binding, question: str, answer_html: str) -> list[dict]:
    """Ask for follow-ups that depend on what this answer actually said."""
    excerpt = plain(answer_html)[:1800]
    if len(excerpt) < 80 or ai_binding is None:
        return []
    messages = [
        {"role": "system", "content": (
            "Write up to 3 useful next questions about ONLY the subject, terms, examples, and steps explicitly present "
            "in the student's question or answer. Never switch to another subject, invent a new application, or ask "
            "something the answer already covered. Each full question must name or clearly refer to one concept from "
            "the supplied answer. Prefer one why/how question, one worked-example question, and one understanding "
            "check when they fit; omit a type if it would be forced or unrelated. Output one line per question: "
            "short action label | full natural question. Labels must be 10 characters or fewer. No numbering or extra text. "
            "Reply in the student's language."
        )},
        {"role": "user", "content": json.dumps({"student_question": str(question)[:300], "answer": excerpt}, ensure_ascii=False)},
    ]
    payload = {
        "messages": messages, "temperature": 0.4, "max_completion_tokens": 260,
        "reasoning_effort": "low", "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        result = await ai_binding.run(course_tutor_ai.TUTOR_MODEL, payload)
        text, _ = course_tutor_ai._completion_parts(result)
        items = parse_suggestions(text or "")
        return _relevant_suggestions(items, question, excerpt)
    except Exception as exc:
        logger.info("followup_suggestions_failed %s", type(exc).__name__)
        return []


def new_key() -> str:
    return secrets.token_hex(4)


async def save(database, user_id, chat_id, items: list[dict], now_ms: int, key: str) -> None:
    await database.prepare(
        """
        INSERT INTO tutor_followup_sets (user_id, chat_id, set_key, items_json, created_at_ms)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, chat_id, set_key) DO UPDATE SET
            items_json = excluded.items_json, created_at_ms = excluded.created_at_ms
        """
    ).bind(int(user_id), str(chat_id), str(key), json.dumps(items, ensure_ascii=False), int(now_ms)).run()


async def load(database, user_id, chat_id, now_ms: int, key: str) -> list[dict]:
    row = await database.prepare(
        "SELECT items_json, created_at_ms FROM tutor_followup_sets WHERE user_id = ? AND chat_id = ? AND set_key = ?"
    ).bind(int(user_id), str(chat_id), str(key)).first()
    if not row or int(row["created_at_ms"]) + TTL_MS < int(now_ms):
        return []
    try:
        return [i for i in json.loads(row["items_json"]) if isinstance(i, dict) and i.get("question")]
    except (TypeError, ValueError):
        return []


def keyboard(items: list[dict], lesson_url: str | None = None, truncated: bool = False, key: str = "") -> dict | None:
    rows: list[list[dict]] = []
    if truncated:
        rows.append([{"text": "▶️ Continue", "callback_data": "fu:c:0"}])
    for index, item in enumerate(items[:MAX_SUGGESTIONS]):
        rows.append([{"text": f"💬 {item['label'][:LABEL_CHARS]}", "callback_data": f"fu:s:{key}:{index}"}])
    if lesson_url:
        rows.append([{"text": "📖 Open visual lesson", "url": lesson_url}])
    return {"inline_keyboard": rows} if rows else None
