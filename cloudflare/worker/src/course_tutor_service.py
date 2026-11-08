"""Course-scoped retrieval and indexing for the Telegram academic tutor."""

import logging
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import course_tutor_ai
import lesson_page_service

CHUNK_SIZE = 1_200
CHUNK_OVERLAP = 200
MAX_SEARCH_TERMS = 12
MAX_PASSAGE_RESULTS = 5
INDEX_LEASE_MS = 60_000
MAX_TUTOR_USER_REQUESTS_PER_DAY = 30
MAX_TUTOR_GROUP_REQUESTS_PER_DAY = 100
MAX_TUTOR_REQUESTS_PER_MONTH = 3_000
TUTOR_USAGE_LIMITS_ENABLED = False
MAX_TUTOR_MEMORY_EXCHANGES = 8
MAX_TUTOR_MEMORY_MESSAGE_CHARS = 2_000
MAX_TUTOR_PREFERENCES_CHARS = 29_000
MAX_TUTOR_PREFERENCE_ENTRY_CHARS = 4_000
MAX_TUTOR_PERSONALITIES = 6
MAX_TUTOR_PERSONALITY_NAME_CHARS = 50
MAX_TUTOR_PERSONALITY_INSTRUCTIONS_CHARS = 4_000
PRIVATE_TUTOR_CONTEXT = "private"
logger = logging.getLogger("academic_assistant_tutor")


@dataclass(frozen=True)
class TutorReply:
    """Tutor text plus an optional shareable visual lesson URL."""

    text: str
    lesson_url: str
    truncated: bool = False
    guided_lesson: bool = False

    @property
    def reply_markup(self) -> dict:
        return {
            "inline_keyboard": [[
                {"text": "📖 Open visual lesson", "url": self.lesson_url},
            ]]
        }


def chunk_source_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split extracted text into bounded, overlapping passages."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return []
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("invalid_chunk_bounds")
    step = chunk_size - overlap
    return [cleaned[start : start + chunk_size] for start in range(0, len(cleaned), step)]


def build_fts_query(query: str) -> str:
    """Turn untrusted user text into a bounded, quoted FTS5 OR query."""
    terms = []
    for term in re.findall(r"[^\W_]{2,}", str(query or "").casefold(), flags=re.UNICODE):
        if term not in terms:
            terms.append(term)
        if len(terms) >= MAX_SEARCH_TERMS:
            break
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def telegram_message_link(chat_id: str, message_id: int, message_link: str | None = None) -> str | None:
    if isinstance(message_link, str) and message_link.startswith("https://t.me/"):
        return message_link
    normalized_chat_id = str(chat_id)
    if normalized_chat_id.startswith("-100"):
        return f"https://t.me/c/{normalized_chat_id[4:]}/{int(message_id)}"
    return None


async def resolve_course_context(database, chat_id: str, thread_id: int | None) -> dict | None:
    """Resolve only an active mapped course in the invoking group's current semester."""
    if thread_id is None:
        return None
    return await database.prepare(
        """
        SELECT g.college_id, g.chat_id, g.current_semester AS semester_label,
               c.id AS course_id, c.course_name, t.thread_id
        FROM academic_groups g
        JOIN colleges col ON col.college_id = g.college_id AND col.status = 'active'
        JOIN group_topics t ON t.chat_id = g.chat_id AND t.thread_id = ?
        JOIN courses c ON c.id = t.course_id AND c.chat_id = g.chat_id
            AND c.college_id = g.college_id AND c.active = 1
        JOIN course_offerings o ON o.chat_id = g.chat_id AND o.college_id = g.college_id
            AND o.course_id = c.id AND o.semester_label = g.current_semester AND o.active = 1
        WHERE g.chat_id = ? AND g.active = 1 AND g.current_semester IS NOT NULL
        """
    ).bind(thread_id, str(chat_id)).first()


async def index_ready_source(database, source_id: int, now_ms: int) -> bool:
    """Replace the indexed passages for a ready source without creating duplicates."""
    source = await database.prepare(
        """
        SELECT id, college_id, chat_id, course_id, semester_label, thread_id,
               source_message_id, message_link, title, extracted_text
        FROM course_quiz_sources
        WHERE id = ? AND extraction_state = 'ready'
        """
    ).bind(source_id).first()
    if not source:
        return False

    passages = chunk_source_text(source.get("extracted_text") or "")
    await database.prepare(
        "DELETE FROM course_tutor_passages WHERE source_id = ?"
    ).bind(source_id).run()
    for position, passage in enumerate(passages):
        await database.prepare(
            """
            INSERT INTO course_tutor_passages (
                source_id, college_id, chat_id, course_id, semester_label, thread_id,
                source_message_id, message_link, title, passage_index, passage_text, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        ).bind(
            source["id"], source["college_id"], source["chat_id"], source["course_id"],
            source["semester_label"], source["thread_id"], source["source_message_id"],
            telegram_message_link(source["chat_id"], source["source_message_id"], source.get("message_link")),
            source["title"], position, passage, now_ms,
        ).run()
    await database.prepare(
        "UPDATE course_tutor_index_jobs SET status = 'indexed', attempt_count = 0, lease_until_ms = NULL, retry_after_ms = NULL, last_error = NULL, updated_at_ms = ? WHERE source_id = ?"
    ).bind(now_ms, source_id).run()
    return bool(passages)


async def claim_next_index_job(database, now_ms: int) -> dict | None:
    """Lease one pending, retryable, or expired passage-index job."""
    return await database.prepare(
        """
        UPDATE course_tutor_index_jobs
        SET status = 'processing', attempt_count = attempt_count + 1,
            lease_until_ms = ?, retry_after_ms = NULL, updated_at_ms = ?
        WHERE source_id = (
            SELECT j.source_id FROM course_tutor_index_jobs j
            JOIN course_quiz_sources s ON s.id = j.source_id AND s.extraction_state = 'ready'
            WHERE j.status = 'pending'
               OR (j.status = 'failed' AND j.retry_after_ms <= ? AND j.attempt_count < 3)
               OR (j.status = 'processing' AND j.lease_until_ms <= ? AND j.attempt_count < 3)
            ORDER BY j.created_at_ms ASC LIMIT 1
        )
        RETURNING source_id, attempt_count
        """
    ).bind(now_ms + INDEX_LEASE_MS, now_ms, now_ms, now_ms).first()


async def process_next_index_job(database, now_ms: int) -> bool:
    job = await claim_next_index_job(database, now_ms)
    if not job:
        return False
    try:
        indexed = await index_ready_source(database, int(job["source_id"]), now_ms)
        if not indexed:
            raise ValueError("empty_source")
    except Exception:
        retry_after = now_ms + 5 * 60_000 if int(job["attempt_count"]) < 3 else None
        await database.prepare(
            "UPDATE course_tutor_index_jobs SET status = 'failed', lease_until_ms = NULL, retry_after_ms = ?, last_error = 'passage_index_failed', updated_at_ms = ? WHERE source_id = ? AND status = 'processing'"
        ).bind(retry_after, now_ms, job["source_id"]).run()
        return False
    return True


async def retrieve_course_passages(
    database,
    chat_id: str,
    thread_id: int | None,
    query: str,
    limit: int = MAX_PASSAGE_RESULTS,
) -> list[dict]:
    """Return ranked passages only for the course mapped to the invoking topic."""
    fts_query = build_fts_query(query)
    if not fts_query:
        return []
    context = await resolve_course_context(database, str(chat_id), thread_id)
    if not context:
        return []
    bounded_limit = max(1, min(int(limit), MAX_PASSAGE_RESULTS))
    result = await database.prepare(
        """
        SELECT p.id, p.source_id, p.college_id, p.chat_id, p.course_id,
               p.semester_label, p.thread_id, p.source_message_id, p.message_link,
               p.title, p.passage_text, bm25(course_tutor_passages_fts) AS relevance
        FROM course_tutor_passages_fts
        JOIN course_tutor_passages p ON p.id = course_tutor_passages_fts.rowid
        JOIN course_quiz_sources s ON s.id = p.source_id AND s.extraction_state = 'ready'
        JOIN academic_groups g ON g.chat_id = p.chat_id AND g.college_id = p.college_id
            AND g.active = 1 AND g.current_semester = p.semester_label
        JOIN colleges col ON col.college_id = p.college_id AND col.status = 'active'
        JOIN courses c ON c.id = p.course_id AND c.chat_id = p.chat_id
            AND c.college_id = p.college_id AND c.active = 1
        JOIN group_topics t ON t.chat_id = p.chat_id AND t.thread_id = p.thread_id
            AND t.course_id = p.course_id
        JOIN course_offerings o ON o.chat_id = p.chat_id AND o.college_id = p.college_id
            AND o.course_id = p.course_id AND o.semester_label = p.semester_label AND o.active = 1
        WHERE course_tutor_passages_fts MATCH ?
          AND p.college_id = ? AND p.chat_id = ? AND p.course_id = ?
          AND p.semester_label = ? AND p.thread_id = ?
        ORDER BY relevance ASC
        LIMIT ?
        """
    ).bind(
        fts_query, context["college_id"], context["chat_id"], context["course_id"],
        context["semester_label"], context["thread_id"], bounded_limit,
    ).all()
    return (result or {}).get("results") or []


def unique_source_passages(passages: list[dict], limit: int = 2) -> list[dict]:
    """Keep the best-ranked passage per original Telegram material for stable citations."""
    selected = []
    seen = set()
    for passage in passages:
        source_id = passage.get("source_id")
        if source_id in seen:
            continue
        seen.add(source_id)
        selected.append(passage)
        if len(selected) >= max(1, int(limit)):
            break
    return selected


def course_tutor_context_key(context: dict) -> str:
    """Identify a course conversation by the exact course topic and semester."""
    return "course:{}:{}:{}".format(
        int(context["course_id"]),
        int(context["thread_id"]),
        str(context["semester_label"])[:80],
    )


async def get_tutor_preferences(database, user_id: int) -> str:
    row = await database.prepare(
        "SELECT study_preferences FROM tutor_user_profiles WHERE user_id = ?"
    ).bind(int(user_id)).first()
    base_preference = str((row or {}).get("study_preferences") or "").strip()
    try:
        personalities = await list_tutor_personalities(database, user_id)
    except Exception:
        logger.exception("tutor_personality_load_failed user_id=%s", user_id)
        personalities = []
    sections = []
    if base_preference:
        sections.append("Base study preference:\n" + base_preference)
    if personalities:
        sections.append(
            "Saved tutor personalities (all coexist; combine compatible style notes instead of replacing one with another):\n"
            + "\n".join(f"- {item['name']}: {item['instructions']}" for item in personalities)
        )
    return "\n\n".join(sections)[:MAX_TUTOR_PREFERENCES_CHARS]


async def list_tutor_personalities(database, user_id: int) -> list[dict]:
    result = await database.prepare(
        "SELECT id, name, instructions, created_at_ms, updated_at_ms FROM tutor_personalities WHERE user_id = ? ORDER BY created_at_ms, id LIMIT ?"
    ).bind(int(user_id), MAX_TUTOR_PERSONALITIES + 1).all()
    return list((result or {}).get("results") or [])


async def add_tutor_personality(
    database, user_id: int, name: str, instructions: str, now_ms: int
) -> dict:
    normalized_name = re.sub(r"\s+", " ", str(name or "")).strip()
    normalized_instructions = str(instructions or "").strip()
    if type(user_id) is not int or user_id <= 0:
        return {"status": "invalid"}
    if not 1 <= len(normalized_name) <= MAX_TUTOR_PERSONALITY_NAME_CHARS:
        return {"status": "invalid_name"}
    if not 1 <= len(normalized_instructions) <= MAX_TUTOR_PERSONALITY_INSTRUCTIONS_CHARS:
        return {"status": "invalid_instructions"}
    name_key = normalized_name.casefold()
    count = await database.prepare(
        "SELECT COUNT(*) AS personality_count FROM tutor_personalities WHERE user_id = ?"
    ).bind(user_id).first()
    if int((count or {}).get("personality_count") or 0) >= MAX_TUTOR_PERSONALITIES:
        return {"status": "limit"}
    row = await database.prepare(
        """
        INSERT INTO tutor_personalities (user_id, name, name_key, instructions, created_at_ms, updated_at_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, name_key) DO NOTHING
        RETURNING id, name, instructions
        """
    ).bind(user_id, normalized_name, name_key, normalized_instructions, int(now_ms), int(now_ms)).first()
    return {"status": "saved", **row} if row else {"status": "duplicate"}


async def delete_tutor_personality(database, user_id: int, personality_id: int) -> bool:
    row = await database.prepare(
        "DELETE FROM tutor_personalities WHERE id = ? AND user_id = ? RETURNING id"
    ).bind(int(personality_id), int(user_id)).first()
    return bool(row)


async def set_tutor_preferences(
    database, user_id: int, preferences: str, now_ms: int
) -> str:
    normalized = str(preferences or "").strip()[:MAX_TUTOR_PREFERENCE_ENTRY_CHARS]
    await database.prepare(
        """
        INSERT INTO tutor_user_profiles (user_id, study_preferences, updated_at_ms)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            study_preferences = excluded.study_preferences,
            updated_at_ms = excluded.updated_at_ms
        """
    ).bind(int(user_id), normalized, int(now_ms)).run()
    return normalized


async def load_tutor_memory(
    database, user_id: int, chat_id: str, context_key: str
) -> list[dict]:
    limit = MAX_TUTOR_MEMORY_EXCHANGES * 2
    result = await database.prepare(
        """
        SELECT role, content FROM tutor_conversation_messages
        WHERE user_id = ? AND chat_id = ? AND context_key = ?
        ORDER BY id DESC LIMIT ?
        """
    ).bind(int(user_id), str(chat_id), str(context_key), limit).all()
    rows = (result or {}).get("results") or []
    return [
        {"role": row["role"], "content": str(row["content"])[:MAX_TUTOR_MEMORY_MESSAGE_CHARS]}
        for row in reversed(rows)
    ]


async def has_guided_lesson_checkpoint(database, user_id: int, chat_id: str, context_key: str) -> bool:
    """Whether this conversation's latest tutor message is waiting for a lesson navigation reply."""
    try:
        history = await load_tutor_memory(database, user_id, chat_id, context_key)
    except Exception:
        return False
    last_assistant = next((turn for turn in reversed(history) if turn.get("role") == "assistant"), None)
    return bool(
        last_assistant
        and course_tutor_ai._GUIDED_CHECKPOINT_MARKER in str(last_assistant.get("content") or "")
    )


async def save_tutor_exchange(
    database,
    user_id: int,
    chat_id: str,
    context_key: str,
    question: str,
    answer: str,
    now_ms: int,
) -> None:
    for role, content in (("user", question), ("assistant", answer)):
        if role == "assistant":
            # A digest keeps whole paragraphs and lists the sections already taught, so a
            # follow-up like "go deeper" builds on the answer instead of repeating it.
            guided = bool(getattr(answer, "guided_lesson", False))
            bounded_content = course_tutor_ai.compact_answer_for_memory(
                content,
                MAX_TUTOR_MEMORY_MESSAGE_CHARS
                - (len(course_tutor_ai._GUIDED_CHECKPOINT_MARKER) + 1 if guided else 0),
            )
            if guided:
                bounded_content = course_tutor_ai._GUIDED_CHECKPOINT_MARKER + "\n" + bounded_content
        else:
            bounded_content = str(content or "").strip()[:MAX_TUTOR_MEMORY_MESSAGE_CHARS]
        if not bounded_content:
            continue
        await database.prepare(
            """
            INSERT INTO tutor_conversation_messages (
                user_id, chat_id, context_key, role, content, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """
        ).bind(
            int(user_id), str(chat_id), str(context_key), role, bounded_content, int(now_ms)
        ).run()
    await database.prepare(
        """
        DELETE FROM tutor_conversation_messages
        WHERE user_id = ? AND chat_id = ? AND context_key = ?
          AND id NOT IN (
              SELECT id FROM tutor_conversation_messages
              WHERE user_id = ? AND chat_id = ? AND context_key = ?
              ORDER BY id DESC LIMIT ?
          )
        """
    ).bind(
        int(user_id), str(chat_id), str(context_key),
        int(user_id), str(chat_id), str(context_key), MAX_TUTOR_MEMORY_EXCHANGES * 2,
    ).run()


async def forget_tutor_user(database, user_id: int) -> None:
    await database.prepare(
        "DELETE FROM tutor_conversation_messages WHERE user_id = ?"
    ).bind(int(user_id)).run()
    await database.prepare(
        "DELETE FROM tutor_user_profiles WHERE user_id = ?"
    ).bind(int(user_id)).run()
    try:
        await database.prepare(
            "DELETE FROM tutor_personalities WHERE user_id = ?"
        ).bind(int(user_id)).run()
    except Exception:
        logger.exception("tutor_personality_delete_failed user_id=%s", user_id)
    for table in ("tutor_documents", "tutor_followups"):
        try:
            await database.prepare(f"DELETE FROM {table} WHERE user_id = ?").bind(int(user_id)).run()
        except Exception:
            # The tables are introduced in migration 0018; tolerate rolling deploys.
            pass


async def consume_tutor_request_budget(database, chat_id: str, user_id: int, now_ms: int) -> bool:
    """Apply tutor usage limits only when the temporary rollout switch is enabled."""
    if not TUTOR_USAGE_LIMITS_ENABLED:
        return True
    usage_date = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date().isoformat()
    row = await database.prepare(
        """
        INSERT INTO tutor_ai_daily_usage (usage_date, chat_id, user_id, request_count, updated_at_ms)
        SELECT ?, ?, ?, 1, ?
        WHERE COALESCE((
            SELECT SUM(request_count) FROM tutor_ai_daily_usage
            WHERE usage_date = ? AND chat_id = ?
        ), 0) < ?
          AND COALESCE((
            SELECT SUM(request_count) FROM tutor_ai_daily_usage
            WHERE substr(usage_date, 1, 7) = ?
          ), 0) < ?
        ON CONFLICT (usage_date, chat_id, user_id) DO UPDATE SET
            request_count = tutor_ai_daily_usage.request_count + 1,
            updated_at_ms = excluded.updated_at_ms
        WHERE tutor_ai_daily_usage.request_count < ?
          AND COALESCE((
            SELECT SUM(request_count) FROM tutor_ai_daily_usage
            WHERE usage_date = excluded.usage_date AND chat_id = excluded.chat_id
          ), 0) < ?
          AND COALESCE((
            SELECT SUM(request_count) FROM tutor_ai_daily_usage
            WHERE substr(usage_date, 1, 7) = substr(excluded.usage_date, 1, 7)
          ), 0) < ?
        RETURNING request_count
        """
    ).bind(
        usage_date, str(chat_id), int(user_id), now_ms,
        usage_date, str(chat_id), MAX_TUTOR_GROUP_REQUESTS_PER_DAY,
        usage_date[:7], MAX_TUTOR_REQUESTS_PER_MONTH,
        MAX_TUTOR_USER_REQUESTS_PER_DAY, MAX_TUTOR_GROUP_REQUESTS_PER_DAY,
        MAX_TUTOR_REQUESTS_PER_MONTH,
    ).first()
    return bool(row)


async def answer_course_question(
    database,
    ai_binding,
    search_api_key: str,
    chat_id: str,
    thread_id: int | None,
    user_id: int,
    question: str,
    now_ms: int,
    lesson_base_url: str = "",
) -> str | TutorReply:
    """Retrieve scoped course evidence, add public web sources, and synthesize a tutor answer."""
    context = await resolve_course_context(database, str(chat_id), thread_id)
    if not context:
        return "Ask this in the mapped course topic so I can search the correct course materials."
    if not isinstance(question, str) or not question.strip():
        return "Use <code>/ask your question</code> in this course topic."
    if len(question) > course_tutor_ai.MAX_QUERY_CHARS:
        return f"Keep the question under {course_tutor_ai.MAX_QUERY_CHARS} characters so I can search it accurately."
    if not await consume_tutor_request_budget(database, str(chat_id), int(user_id), now_ms):
        return "This group has reached its daily tutor limit, or you have used your daily allowance. Try again tomorrow."

    # Read memory first: a bare follow-up such as "go deeper" is only searchable once it is
    # resolved against the conversation, and history is filtered to what is relevant.
    try:
        preferences, history = await _read_tutor_context(
            database, user_id, str(chat_id), course_tutor_context_key(context)
        )
    except Exception as exc:
        logger.warning("course_tutor_memory_read_failed: %s", type(exc).__name__)
        preferences, history = "", []
    request = course_tutor_ai.analyze_request(question, history)
    should_search = course_tutor_ai.should_search_web(question) or (
        request.kind != "new" and course_tutor_ai.should_search_web(request.topic)
    )
    tasks = [retrieve_course_passages(database, chat_id, thread_id, request.retrieval_query)]
    if should_search:
        tasks.append(course_tutor_ai.search_web(request.retrieval_query, search_api_key, include_images=True))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    passages_result = results[0]
    if isinstance(passages_result, Exception):
        logger.warning("course_tutor_retrieval_failed: %s", type(passages_result).__name__)
        passages_result = []
    passages = unique_source_passages(passages_result)
    web_search_unavailable = False
    web_sources = []
    web_search_attempted = should_search
    if should_search:
        search_result = results[1]
        if isinstance(search_result, Exception):
            logger.warning("course_tutor_web_search_failed: %s", type(search_result).__name__)
            web_search_unavailable = True
        else:
            web_sources = search_result
    if not passages and not web_sources:
        if web_search_unavailable:
            return (
                f"I couldn't find this in the materials for {context['course_name']}, "
                "and live web search isn't configured right now. Try again later or share the relevant course material."
            )
        return (
            f"I couldn't find this in the materials for {context['course_name']}, "
            "and I couldn't find reliable outside sources. Try a more specific question or share relevant material."
        )
    try:
        messages = course_tutor_ai.build_tutor_messages(
            context["course_name"], context["semester_label"], question, passages, web_sources,
            conversation_history=history,
            study_preferences=preferences,
            request_analysis=request,
        )
        answer = await course_tutor_ai.generate_tutor_answer(ai_binding, messages)
    except Exception:
        answer = None
    if not answer:
        return "I found source material, but couldn't prepare a reliable explanation right now. Please try again shortly."
    save_task = save_tutor_exchange(
        database, user_id, str(chat_id), course_tutor_context_key(context),
        question, answer, now_ms,
    )
    truncated = bool(getattr(answer, "truncated", False))
    guided_lesson = bool(getattr(answer, "guided_lesson", False))
    material_sources = []
    for passage in passages:
        material_sources.append({"title": passage["title"], "message_link": passage.get("message_link")})
    response = course_tutor_ai.render_tutor_answer(
        answer,
        context["course_name"],
        material_sources,
        web_sources,
        semester_label=context["semester_label"],
        web_search_unavailable=web_search_unavailable,
        web_search_no_results=(web_search_attempted and not web_sources and not web_search_unavailable),
    )
    offer_lesson = bool(lesson_base_url and course_tutor_ai.should_offer_visual_lesson(question, answer))
    if offer_lesson:
        try:
            lesson_task = lesson_page_service.create_visual_lesson(
                database, _lesson_question(question, request), answer, web_sources, now_ms, lesson_base_url,
                course_material_used=bool(material_sources),
            )
            save_result, lesson_url = await asyncio.gather(save_task, lesson_task, return_exceptions=True)
            if isinstance(save_result, Exception):
                logger.warning("course_tutor_memory_save_failed: %s", type(save_result).__name__)
            if isinstance(lesson_url, Exception):
                logger.warning("course_visual_lesson_create_failed: %s", type(lesson_url).__name__)
                lesson_url = None
            if lesson_url:
                return TutorReply(_hide_chat_source_list(response), lesson_url, truncated, guided_lesson)
        except Exception:
            logger.exception("course_visual_lesson_create_failed")
    if not offer_lesson:
        try:
            await save_task
        except Exception:
            pass
    return course_tutor_ai.tag_text(response, truncated, guided_lesson)


async def answer_general_question(
    database,
    ai_binding,
    search_api_key: str,
    chat_id: str,
    user_id: int,
    question: str,
    now_ms: int,
    attachment_context: dict | None = None,
    allow_visual_lesson: bool = True,
    lesson_base_url: str = "",
) -> str | TutorReply:
    """Answer a private general study question, with optional live web citations."""
    if not isinstance(question, str) or not question.strip():
        return "Send a study question, or use <code>/ask your question</code>."
    if len(question) > course_tutor_ai.MAX_QUERY_CHARS:
        return f"Keep your question under {course_tutor_ai.MAX_QUERY_CHARS} characters so I can answer it accurately."
    short_reply = course_tutor_ai.explicit_short_reply(question)
    if short_reply is not None:
        return short_reply
    if ai_binding is None:
        return "The tutor is temporarily unavailable. Please try again shortly."
    if not await consume_tutor_request_budget(database, str(chat_id), int(user_id), now_ms):
        return "You've used your 30 daily study questions. Your allowance resets at 00:00 UTC."

    web_search_unavailable = False
    web_sources = []
    attachment_requests_web = bool(
        re.search(r"\b(latest|current|today|web|online|sources?|citations?|research)\b", question, re.IGNORECASE)
    )
    try:
        preferences, history = await _read_tutor_context(database, user_id, str(chat_id), PRIVATE_TUTOR_CONTEXT)
    except Exception:
        preferences, history = "", []
    request = course_tutor_ai.analyze_request(question, history)
    should_search = (
        course_tutor_ai.should_search_web(question)
        or (request.kind != "new" and course_tutor_ai.should_search_web(request.topic))
    ) and (attachment_context is None or attachment_requests_web)
    web_search_attempted = should_search
    if should_search:
        try:
            web_sources = await course_tutor_ai.search_web(
                request.retrieval_query, search_api_key, include_images=True
            )
        except Exception as exc:
            logger.warning("general_tutor_web_search_failed: %s", type(exc).__name__)
            web_search_unavailable = True
            web_sources = []
    try:
        messages = course_tutor_ai.build_general_tutor_messages(
            question, web_sources,
            conversation_history=history,
            study_preferences=preferences,
            attachment_context=attachment_context,
            request_analysis=request,
        )
        answer = await course_tutor_ai.generate_tutor_answer(ai_binding, messages)
    except Exception:
        answer = None
    if not answer:
        return "I couldn't prepare a reliable explanation right now. Please try again shortly."
    stored_question = question
    if isinstance(attachment_context, dict) and attachment_context.get("name"):
        stored_question = f"[Attached file: {str(attachment_context['name'])[:160]}] {question}"
    save_task = save_tutor_exchange(
        database, user_id, str(chat_id), PRIVATE_TUTOR_CONTEXT,
        stored_question, answer, now_ms,
    )
    truncated = bool(getattr(answer, "truncated", False))
    guided_lesson = bool(getattr(answer, "guided_lesson", False))
    response = course_tutor_ai.render_general_tutor_answer(
        answer,
        web_sources,
        web_search_unavailable=web_search_unavailable,
        web_search_no_results=(web_search_attempted and not web_sources and not web_search_unavailable),
    )
    if (
        allow_visual_lesson
        and lesson_base_url
        and course_tutor_ai.should_offer_visual_lesson(question, answer)
    ):
        try:
            save_result, lesson_url = await asyncio.gather(
                save_task,
                lesson_page_service.create_visual_lesson(
                    database, _lesson_question(question, request), answer, web_sources, now_ms, lesson_base_url
                ),
                return_exceptions=True,
            )
            if isinstance(save_result, Exception):
                logger.warning("general_tutor_memory_save_failed: %s", type(save_result).__name__)
            if isinstance(lesson_url, Exception):
                logger.warning("general_visual_lesson_create_failed: %s", type(lesson_url).__name__)
                lesson_url = None
            if lesson_url:
                return TutorReply(_hide_chat_source_list(response), lesson_url, truncated, guided_lesson)
        except Exception:
            logger.exception("general_visual_lesson_create_failed")
    if not (
        allow_visual_lesson and lesson_base_url
        and course_tutor_ai.should_offer_visual_lesson(question, answer)
    ):
        try:
            await save_task
        except Exception:
            pass
    return course_tutor_ai.tag_text(response, truncated, guided_lesson)


def _lesson_question(question: str, request) -> str:
    """Give a bare follow-up ('go deeper') a meaningful lesson title by naming its topic."""
    topic = getattr(request, "topic", "")
    if getattr(request, "kind", "new") in {"new", "solve"} or not topic:
        return question
    return f"{topic} - {question}"[: course_tutor_ai.MAX_QUERY_CHARS]


def _hide_chat_source_list(response: str) -> str:
    """Keep the chat reply concise when detailed citations are on the lesson page."""
    marker = "\n\n🌐 <b>Additional sources</b>"
    index = response.find(marker)
    return response[:index].rstrip() if index >= 0 else response


async def _read_tutor_context(database, user_id: int, chat_id: str, context_key: str):
    """Load optional personalized context without making tutor availability depend on D1."""
    try:
        preferences = await get_tutor_preferences(database, user_id)
    except Exception:
        preferences = ""
    try:
        history = await load_tutor_memory(database, user_id, chat_id, context_key)
    except Exception:
        history = []
    return preferences, history
