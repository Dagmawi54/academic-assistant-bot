"""Context-aware buttons: follow-up questions written from the answer itself, plus Continue only when cut off."""

import json
import logging
import re
import secrets

import course_tutor_ai

logger = logging.getLogger("academic_assistant_followups")

MAX_SUGGESTIONS = 3
LABEL_CHARS = 18
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
            items.append({"label": label[:LABEL_CHARS].rstrip(" .…"), "question": question[:QUESTION_CHARS]})
        if len(items) == MAX_SUGGESTIONS:
            break
    return items


async def suggest(ai_binding, question: str, answer_html: str) -> list[dict]:
    """Ask for follow-ups that depend on what this answer actually said."""
    excerpt = plain(answer_html)[:1800]
    if len(excerpt) < 80 or ai_binding is None:
        return []
    messages = [
        {"role": "system", "content": (
            "You suggest what a student would naturally ask NEXT after reading the answer below. Write exactly 3 "
            "follow-up questions that refer to specific things the answer mentioned (a term, step, example or "
            "code line), each of a different kind: one that digs into why/how, one that applies it to an example "
            "or exercise, one that checks understanding or covers a common mistake. Never suggest something the "
            "answer already covered. Output only 3 lines, each: short button label (max 30 characters) | the full "
            "question written in the student's voice. No numbering, no extra text. Reply in the student's language."
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
        return parse_suggestions(text or "")
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
