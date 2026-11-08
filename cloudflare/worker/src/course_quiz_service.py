"""Semester-scoped automatic quizzes sourced from mapped Telegram course topics."""

import hashlib
import io
import json
import re
import xml.etree.ElementTree as ElementTree
import zipfile
from datetime import date, datetime, timedelta, timezone

ADDIS_OFFSET = timedelta(hours=3)
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_CHARS = 50_000
MIN_COURSE_SOURCE_CHARS = 1_500
MAX_GENERATION_SOURCE_CHARS = 18_000
GENERATION_LEASE_MS = 60_000
DELIVERY_BATCH_SIZE = 1
AI_MODEL = "@cf/zai-org/glm-4.7-flash"
DAILY_EXTRACTION_CALL_LIMIT = 25
DAILY_GENERATION_CALL_LIMIT = 10


def normalize_course_name(name: str) -> str:
    """Normalize display names consistently for course mapping and topic matching."""
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


def parse_semester_set_command(text: str) -> str | None:
    match = re.fullmatch(r"\s*/semester_set(?:@\w+)?\s+(.+?)\s*", text, re.IGNORECASE)
    if not match:
        return None
    label = re.sub(r"\s+", " ", match.group(1)).strip()
    return label if 1 <= len(label) <= 40 else None


def parse_quiz_source_command(text: str) -> bool:
    return bool(re.fullmatch(r"\s*/quiz_source(?:@\w+)?\s*", text, re.IGNORECASE))


def parse_quiz_topic_command(text: str) -> bool:
    return bool(re.fullmatch(r"\s*/quiz_topic(?:@\w+)?\s*", text, re.IGNORECASE))


def _addis_datetime(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc) + ADDIS_OFFSET


def is_course_quiz_window(now_ms: int) -> bool:
    local = _addis_datetime(now_ms)
    return local.hour == 10 and local.date().toordinal() % 2 == 0


def exam_is_tomorrow(now_ms: int, deadline_ms: int) -> bool:
    return _addis_datetime(deadline_ms).date() == _addis_datetime(now_ms).date() + timedelta(days=1)


def addis_day_window(day: date) -> tuple[int, int]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone(ADDIS_OFFSET))
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


async def consume_daily_ai_budget(database, now_ms: int, operation: str) -> bool:
    usage_date = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date().isoformat()
    if operation == "extraction":
        query = """
        INSERT INTO quiz_ai_daily_usage (usage_date, extraction_calls, generation_calls, updated_at_ms)
        VALUES (?, 1, 0, ?)
        ON CONFLICT(usage_date) DO UPDATE SET
            extraction_calls = quiz_ai_daily_usage.extraction_calls + 1, updated_at_ms = excluded.updated_at_ms
        WHERE quiz_ai_daily_usage.extraction_calls < ?
        RETURNING usage_date
        """
        values = (usage_date, now_ms, DAILY_EXTRACTION_CALL_LIMIT)
    elif operation == "generation":
        query = """
        INSERT INTO quiz_ai_daily_usage (usage_date, extraction_calls, generation_calls, updated_at_ms)
        VALUES (?, 0, 1, ?)
        ON CONFLICT(usage_date) DO UPDATE SET
            generation_calls = quiz_ai_daily_usage.generation_calls + 1, updated_at_ms = excluded.updated_at_ms
        WHERE quiz_ai_daily_usage.generation_calls < ?
        RETURNING usage_date
        """
        values = (usage_date, now_ms, DAILY_GENERATION_CALL_LIMIT)
    else:
        raise ValueError("unknown_ai_operation")
    result = await database.prepare(query).bind(*values).first()
    return bool(result)


async def daily_ai_usage(database, now_ms: int) -> dict:
    usage_date = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date().isoformat()
    row = await database.prepare(
        "SELECT extraction_calls, generation_calls FROM quiz_ai_daily_usage WHERE usage_date = ?"
    ).bind(usage_date).first()
    return row or {"extraction_calls": 0, "generation_calls": 0}


def _normalize_source_quote(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def validate_generated_questions(
    value,
    expected_count: int,
    valid_source_ids: set[int],
    source_text_by_id: dict[int, str] | None = None,
    allow_fewer: bool = False,
) -> list[dict] | None:
    if isinstance(value, dict):
        value = value.get("questions")
    if (
        not isinstance(value, list)
        or len(value) > expected_count
        or (len(value) != expected_count and not allow_fewer)
        or not value
    ):
        return None
    questions = []
    for item in value:
        if not isinstance(item, dict):
            return None
        prompt = item.get("prompt")
        options = item.get("options")
        correct = item.get("correct_option_index")
        source_id = item.get("source_message_id")
        excerpt = item.get("source_excerpt")
        if (
            not isinstance(prompt, str)
            or not 5 <= len(prompt.strip()) <= 300
            or not isinstance(options, list)
            or len(options) != 4
            or any(not isinstance(option, str) or not 1 <= len(option.strip()) <= 100 for option in options)
            or len({option.strip().casefold() for option in options}) != 4
            or type(correct) is not int
            or correct not in range(4)
            or type(source_id) is not int
            or source_id not in valid_source_ids
            or not isinstance(excerpt, str)
            or not 8 <= len(excerpt.strip()) <= 500
        ):
            return None
        if source_text_by_id is not None:
            if _normalize_source_quote(excerpt) not in _normalize_source_quote(source_text_by_id.get(source_id, "")):
                return None
        questions.append({
            "prompt": prompt.strip(),
            "options": [option.strip() for option in options],
            "correct_option_index": correct,
            "source_message_id": source_id,
            "source_excerpt": excerpt.strip(),
        })
    return questions


def build_generation_messages(course_name: str, semester_label: str, sources: list[dict], count: int) -> list[dict]:
    excerpts = []
    remaining = MAX_GENERATION_SOURCE_CHARS
    for source in sources:
        text = str(source.get("extracted_text") or "")[:remaining]
        if not text:
            continue
        excerpts.append({"source_message_id": int(source["source_message_id"]), "text": text})
        remaining -= len(text)
        if remaining <= 0:
            break
    system = (
        "Create accurate university-level multiple-choice questions only from the supplied course materials. "
        "Do not use outside knowledge or invent facts. Return JSON only with a questions array. Each question "
        "must include prompt, exactly four options, correct_option_index (zero-based), source_message_id copied "
        "from a supplied source, and a short verbatim source_excerpt supporting the answer."
    )
    user = json.dumps({
        "course": course_name,
        "semester": semester_label,
        "question_count": count,
        "questions": [{
            "prompt": "string", "options": ["A", "B", "C", "D"],
            "correct_option_index": 0, "source_message_id": 123, "source_excerpt": "string",
        }],
        "materials": excerpts,
    }, ensure_ascii=False)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def activate_course_offering(database, chat_id: str, course_id: int, college_id: str, semester_label: str, now_ms: int) -> bool:
    if not semester_label:
        return False
    result = await database.prepare(
        """
        INSERT INTO course_offerings (college_id, chat_id, course_id, semester_label, active, created_at_ms)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(chat_id, course_id, semester_label) DO UPDATE SET active = 1
        RETURNING course_id
        """
    ).bind(college_id, chat_id, course_id, semester_label, now_ms).first()
    return bool(result)


async def set_group_semester(database, chat_id: str, semester_label: str, now_ms: int) -> bool:
    result = await database.prepare(
        "UPDATE academic_groups SET current_semester = ? WHERE chat_id = ? AND active = 1 RETURNING chat_id"
    ).bind(semester_label, chat_id).first()
    if not result:
        return False
    group = await database.prepare(
        "SELECT college_id FROM academic_groups WHERE chat_id = ? AND active = 1"
    ).bind(chat_id).first()
    if group:
        await database.prepare(
            """
            INSERT INTO course_offerings (college_id, chat_id, course_id, semester_label, active, created_at_ms)
            SELECT g.college_id, g.chat_id, t.course_id, g.current_semester, 1, ?
            FROM academic_groups g JOIN group_topics t ON t.chat_id = g.chat_id
            WHERE g.chat_id = ? AND g.active = 1 AND g.current_semester = ?
            ON CONFLICT(chat_id, course_id, semester_label) DO UPDATE SET active = 1
            """
        ).bind(now_ms, chat_id, semester_label).run()
    return True


async def set_quiz_topic(database, chat_id: str, thread_id: int, college_id: str, user_id: int, now_ms: int) -> bool:
    result = await database.prepare(
        """
        INSERT INTO quiz_topic_settings (chat_id, college_id, thread_id, set_by_user_id, updated_at_ms)
        SELECT chat_id, college_id, ?, ?, ? FROM academic_groups
        WHERE chat_id = ? AND college_id = ? AND active = 1
        ON CONFLICT(chat_id) DO UPDATE SET college_id = excluded.college_id,
            thread_id = excluded.thread_id, set_by_user_id = excluded.set_by_user_id,
            updated_at_ms = excluded.updated_at_ms
        RETURNING chat_id
        """
    ).bind(thread_id, user_id, now_ms, chat_id, college_id).first()
    return bool(result)


async def save_quiz_file_source(database, chat_id: str, resource: dict, now_ms: int) -> bool:
    mime_type = str(resource.get("mime_type") or "").lower()
    filename = str(resource.get("title") or "").lower()
    is_pdf = mime_type == "application/pdf" or filename.endswith(".pdf")
    is_image = mime_type.startswith("image/") or resource.get("resource_type") == "photo"
    if not (is_pdf or is_image) or not resource.get("file_id"):
        return False
    mapped = await database.prepare(
        """
        SELECT g.college_id, g.current_semester, c.id AS course_id, c.course_name
        FROM academic_groups g
        JOIN colleges col ON col.college_id = g.college_id AND col.status = 'active'
        JOIN group_topics t ON t.chat_id = g.chat_id AND t.thread_id = ?
        JOIN courses c ON c.id = t.course_id AND c.chat_id = g.chat_id AND c.college_id = g.college_id AND c.active = 1
        JOIN course_offerings o ON o.chat_id = g.chat_id AND o.course_id = c.id
            AND o.college_id = g.college_id AND o.semester_label = g.current_semester AND o.active = 1
        WHERE g.chat_id = ? AND g.active = 1 AND g.current_semester IS NOT NULL
        """
    ).bind(resource.get("thread_id"), chat_id).first()
    if not mapped:
        return False
    state = "pending" if not resource.get("file_size") or resource["file_size"] <= MAX_FILE_BYTES else "ignored"
    error = None if state == "pending" else "file_too_large"
    result = await database.prepare(
        """
        INSERT INTO course_quiz_sources (
            college_id, chat_id, course_id, semester_label, thread_id, source_message_id,
            message_link, resource_type, title, caption, file_id, mime_type, file_size,
            extraction_state, last_error, created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, source_message_id) DO NOTHING
        RETURNING id
        """
    ).bind(
        mapped["college_id"], chat_id, mapped["course_id"], mapped["current_semester"],
        resource["thread_id"], resource["source_message_id"], resource.get("message_link"),
        resource["resource_type"], resource["title"], resource.get("caption"), resource["file_id"],
        mime_type or None, resource.get("file_size"), state, error, now_ms, now_ms,
    ).first()
    return bool(result)


async def save_quiz_text_source(database, chat_id: str, thread_id: int, source_message: dict, user_id: int, now_ms: int) -> bool:
    text = source_message.get("text")
    message_id = source_message.get("message_id")
    if not isinstance(text, str) or not 20 <= len(text.strip()) <= MAX_EXTRACTED_CHARS or not isinstance(message_id, int):
        return False
    source_thread = source_message.get("message_thread_id")
    if source_thread != thread_id:
        return False
    mapped = await database.prepare(
        """
        SELECT g.college_id, g.current_semester, c.id AS course_id
        FROM academic_groups g
        JOIN colleges col ON col.college_id = g.college_id AND col.status = 'active'
        JOIN group_topics t ON t.chat_id = g.chat_id AND t.thread_id = ?
        JOIN courses c ON c.id = t.course_id AND c.chat_id = g.chat_id AND c.college_id = g.college_id AND c.active = 1
        JOIN course_offerings o ON o.chat_id = g.chat_id AND o.course_id = c.id
            AND o.college_id = g.college_id AND o.semester_label = g.current_semester AND o.active = 1
        WHERE g.chat_id = ? AND g.active = 1 AND g.current_semester IS NOT NULL
        """
    ).bind(thread_id, chat_id).first()
    if not mapped:
        return False
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    result = await database.prepare(
        """
        INSERT INTO course_quiz_sources (
            college_id, chat_id, course_id, semester_label, thread_id, source_message_id,
            resource_type, title, extracted_text, content_sha256, extraction_state,
            created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, 'note', ?, ?, ?, 'ready', ?, ?)
        ON CONFLICT(chat_id, source_message_id) DO NOTHING
        RETURNING id
        """
    ).bind(
        mapped["college_id"], chat_id, mapped["course_id"], mapped["current_semester"],
        thread_id, message_id, "Class note", text.strip(), digest, now_ms, now_ms,
    ).first()
    return bool(result)


async def claim_extraction_source(database, now_ms: int) -> dict | None:
    await database.prepare(
        "UPDATE course_quiz_sources SET extraction_state = 'ignored', lease_until_ms = NULL, last_error = 'extraction_attempts_exhausted' WHERE extraction_state = 'processing' AND lease_until_ms <= ? AND attempt_count >= 3"
    ).bind(now_ms).run()
    return await database.prepare(
        """
        UPDATE course_quiz_sources
        SET extraction_state = 'processing', attempt_count = attempt_count + 1,
            lease_until_ms = ?, retry_after_ms = NULL
        WHERE id = (
            SELECT s.id FROM course_quiz_sources s
            JOIN academic_groups g ON g.chat_id = s.chat_id AND g.college_id = s.college_id
                AND g.active = 1 AND g.current_semester = s.semester_label
            JOIN colleges col ON col.college_id = s.college_id AND col.status = 'active'
            JOIN courses c ON c.id = s.course_id AND c.chat_id = s.chat_id
                AND c.college_id = s.college_id AND c.active = 1
            JOIN group_topics t ON t.chat_id = s.chat_id AND t.thread_id = s.thread_id AND t.course_id = c.id
            JOIN course_offerings o ON o.chat_id = s.chat_id AND o.course_id = s.course_id
                AND o.college_id = s.college_id AND o.semester_label = s.semester_label AND o.active = 1
            WHERE s.extraction_state = 'pending'
               OR (s.extraction_state = 'failed' AND s.retry_after_ms <= ? AND s.attempt_count < 3)
               OR (s.extraction_state = 'processing' AND s.lease_until_ms <= ? AND s.attempt_count < 3)
            ORDER BY s.created_at_ms ASC LIMIT 1
        )
        RETURNING id, college_id, chat_id, course_id, semester_label, thread_id,
                  source_message_id, file_id, mime_type, file_size, title, caption, attempt_count
        """
    ).bind(now_ms + GENERATION_LEASE_MS, now_ms, now_ms).first()


async def finish_source_extraction(database, source_id: int, now_ms: int, text: str | None, error: str | None = None) -> None:
    cleaned = re.sub(r"\n{3,}", "\n\n", (text or "")).strip()[:MAX_EXTRACTED_CHARS]
    if cleaned and len(cleaned) >= 80:
        checksum = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        await database.prepare(
            "UPDATE course_quiz_sources SET extracted_text = ?, content_sha256 = ?, extraction_state = 'ready', lease_until_ms = NULL, retry_after_ms = NULL, last_error = NULL, updated_at_ms = ? WHERE id = ? AND extraction_state = 'processing'"
        ).bind(cleaned, checksum, now_ms, source_id).run()
        return
    if error and error not in {"unsupported_file", "file_too_large", "empty_extraction", "low_text_extraction"}:
        await database.prepare(
            "UPDATE course_quiz_sources SET extraction_state = CASE WHEN attempt_count >= 3 THEN 'ignored' ELSE 'failed' END, lease_until_ms = NULL, retry_after_ms = ?, last_error = ?, updated_at_ms = ? WHERE id = ? AND extraction_state = 'processing'"
        ).bind(now_ms + 5 * 60_000, error[:80], now_ms, source_id).run()
        return
    await database.prepare(
        "UPDATE course_quiz_sources SET extraction_state = 'ignored', lease_until_ms = NULL, retry_after_ms = NULL, last_error = ?, updated_at_ms = ? WHERE id = ? AND extraction_state = 'processing'"
    ).bind(error or "low_text_extraction", now_ms, source_id).run()


async def _download_telegram_file(token: str, source: dict):
    from workers import fetch

    if source.get("file_size") and source["file_size"] > MAX_FILE_BYTES:
        raise ValueError("file_too_large")
    metadata_response = await fetch(
        f"https://api.telegram.org/bot{token}/getFile?file_id={source['file_id']}"
    )
    metadata = await metadata_response.json()
    if metadata_response.status >= 400 or not metadata.get("ok"):
        raise RuntimeError("telegram_file_lookup_failed")
    file_info = metadata.get("result") or {}
    remote_size = file_info.get("file_size")
    if isinstance(remote_size, (int, float)) and remote_size > MAX_FILE_BYTES:
        raise ValueError("file_too_large")
    file_path = file_info.get("file_path")
    if not isinstance(file_path, str) or file_path.startswith("/") or ".." in file_path.split("/"):
        raise ValueError("unsupported_file")
    file_response = await fetch(f"https://api.telegram.org/file/bot{token}/{file_path}")
    if file_response.status >= 400:
        raise RuntimeError("telegram_file_download_failed")
    # Cloudflare's Python FetchResponse wraps JavaScript Response bodies with
    # Pyodide's Python API; `bytes()` is the supported binary reader here.
    data = await file_response.bytes()
    data_size = len(data)
    if data_size == 0 or data_size > MAX_FILE_BYTES:
        raise ValueError("file_too_large" if data_size > MAX_FILE_BYTES else "empty_extraction")
    mime = source.get("mime_type") or ("application/pdf" if str(source.get("title", "")).lower().endswith(".pdf") else "image/jpeg")
    return data, mime


async def _convert_file_to_markdown(ai_binding, source: dict, data, mime: str) -> str:
    from js import Object
    from pyodide.ffi import to_js
    from workers import Blob

    blob = Blob(data, content_type=mime)
    document = to_js(
        {"name": source["title"][:200], "blob": blob.js_object},
        dict_converter=Object.fromEntries,
    )
    converted = await ai_binding.toMarkdown(to_js([document]))
    if hasattr(converted, "to_py"):
        converted = converted.to_py()
    documents = converted if isinstance(converted, list) else [converted] if isinstance(converted, dict) else []
    if documents and hasattr(documents[0], "to_py"):
        documents[0] = documents[0].to_py()
    if not documents or not isinstance(documents[0], dict):
        raise ValueError("empty_extraction")
    if documents[0].get("format") == "error":
        # Cloudflare returns structured conversion failures rather than raising.
        # Keep the reason in Worker logs, without logging user file contents.
        reason = re.sub(r"\s+", " ", str(documents[0].get("error") or "unknown"))[:160]
        raise RuntimeError(f"cloudflare_markdown_conversion_failed:{reason}")
    result_text = documents[0].get("data") or documents[0].get("markdown") or documents[0].get("text") or ""
    if not isinstance(result_text, str):
        raise ValueError("empty_extraction")
    return result_text


def _extract_pptx_slide_text(data) -> str:
    """Extract readable text from a PPTX without a native office binary or local storage."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(data)
    else:
        from js import Uint8Array

        raw_bytes = bytes(Uint8Array.new(data).to_py())
    slides = []
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as package:
        slide_files = []
        expanded_size = 0
        for item in package.infolist():
            match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", item.filename)
            if not match:
                continue
            expanded_size += item.file_size
            if item.file_size > 256_000 or expanded_size > 2_000_000:
                raise ValueError("unsupported_file")
            slide_files.append((int(match.group(1)), item))
        for slide_number, item in sorted(slide_files, key=lambda entry: entry[0]):
            root = ElementTree.fromstring(package.read(item))
            text_parts = [
                node.text.strip()
                for node in root.iter()
                if node.tag.endswith("}t") and isinstance(node.text, str) and node.text.strip()
            ]
            if text_parts:
                slides.append(f"Slide {slide_number}\n" + "\n".join(text_parts))
    text = "\n\n".join(slides).strip()
    if not text:
        raise ValueError("empty_extraction")
    return text[:MAX_EXTRACTED_CHARS]


async def extract_telegram_file_source(token: str, ai_binding, source: dict) -> str:
    data, mime = await _download_telegram_file(token, source)
    if str(source.get("title") or "").lower().endswith(".pptx"):
        return _extract_pptx_slide_text(data)
    return await _convert_file_to_markdown(ai_binding, source, data, mime)


async def course_sources(database, chat_id: str, course_id: int, semester_label: str) -> list[dict]:
    rows = await database.prepare(
        """
        SELECT s.source_message_id, s.extracted_text
        FROM course_quiz_sources s
        JOIN academic_groups g ON g.chat_id = s.chat_id AND g.college_id = s.college_id AND g.active = 1
        JOIN colleges col ON col.college_id = s.college_id AND col.status = 'active'
        JOIN courses c ON c.id = s.course_id AND c.chat_id = s.chat_id AND c.college_id = s.college_id AND c.active = 1
        JOIN group_topics t ON t.chat_id = s.chat_id AND t.thread_id = s.thread_id AND t.course_id = c.id
        JOIN course_offerings o ON o.chat_id = s.chat_id AND o.course_id = c.id
            AND o.college_id = s.college_id AND o.semester_label = s.semester_label AND o.active = 1
        WHERE s.chat_id = ? AND s.course_id = ? AND s.semester_label = ?
          AND g.current_semester = s.semester_label AND s.extraction_state = 'ready'
        ORDER BY s.created_at_ms DESC LIMIT 30
        """
    ).bind(chat_id, course_id, semester_label).all()
    sources = rows["results"] or []
    if sum(len(row.get("extracted_text") or "") for row in sources) < MIN_COURSE_SOURCE_CHARS:
        return []
    return sources


async def queue_due_runs(database, now_ms: int) -> int:
    local = _addis_datetime(now_ms)
    day_key = local.date().isoformat()
    queued = 0
    if local.hour >= 10 and local.date().toordinal() % 2 == 0:
        groups = await database.prepare(
            """
            SELECT g.chat_id, g.college_id, g.current_semester, q.thread_id
            FROM academic_groups g JOIN quiz_topic_settings q ON q.chat_id = g.chat_id AND q.college_id = g.college_id
            JOIN colleges col ON col.college_id = g.college_id AND col.status = 'active'
            WHERE g.active = 1 AND g.current_semester IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM quiz_generation_runs r WHERE r.chat_id = g.chat_id AND r.run_key = ?)
              AND EXISTS (
                  SELECT 1 FROM course_offerings o
                  JOIN courses c ON c.id = o.course_id AND c.chat_id = g.chat_id AND c.college_id = g.college_id AND c.active = 1
                  JOIN group_topics t ON t.chat_id = g.chat_id AND t.course_id = c.id
                  JOIN course_quiz_sources s ON s.chat_id = g.chat_id AND s.course_id = c.id AND s.thread_id = t.thread_id
                      AND s.college_id = g.college_id AND s.semester_label = o.semester_label AND s.extraction_state = 'ready'
                  WHERE o.chat_id = g.chat_id AND o.college_id = g.college_id
                    AND o.semester_label = g.current_semester AND o.active = 1
                  GROUP BY c.id HAVING SUM(LENGTH(s.extracted_text)) >= ?
              )
            ORDER BY g.chat_id LIMIT 1
            """
        ).bind(f"regular:{day_key}", MIN_COURSE_SOURCE_CHARS).all()
        for group in groups["results"] or []:
            course = await database.prepare(
                """
                SELECT c.id AS course_id, c.course_name
                FROM courses c
                JOIN course_offerings o ON o.course_id = c.id AND o.chat_id = c.chat_id AND o.college_id = c.college_id
                    AND o.semester_label = ? AND o.active = 1
                JOIN group_topics t ON t.chat_id = c.chat_id AND t.course_id = c.id
                JOIN course_quiz_sources s ON s.chat_id = c.chat_id AND s.course_id = c.id AND s.thread_id = t.thread_id
                    AND s.college_id = c.college_id AND s.semester_label = o.semester_label AND s.extraction_state = 'ready'
                WHERE c.chat_id = ? AND c.college_id = ? AND c.active = 1
                GROUP BY c.id
                HAVING SUM(LENGTH(s.extracted_text)) >= ?
                ORDER BY COALESCE((SELECT MAX(r.updated_at_ms) FROM quiz_generation_runs r
                    WHERE r.chat_id = c.chat_id AND r.course_id = c.id AND r.status = 'completed'), 0), c.id
                LIMIT 1
                """
            ).bind(group["current_semester"], group["chat_id"], group["college_id"], MIN_COURSE_SOURCE_CHARS).first()
            if not course:
                continue
            run = await database.prepare(
                """
                INSERT INTO quiz_generation_runs (
                    college_id, chat_id, semester_label, course_id, target_thread_id,
                    run_kind, run_key, status, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, 'regular', ?, 'pending', ?, ?)
                ON CONFLICT(chat_id, run_key) DO NOTHING RETURNING id
                """
            ).bind(
                group["college_id"], group["chat_id"], group["current_semester"], course["course_id"],
                group["thread_id"], f"regular:{day_key}", now_ms, now_ms,
            ).first()
            queued += int(bool(run))
    if local.hour >= 16:
        tomorrow = local.date() + timedelta(days=1)
        start_ms, end_ms = addis_day_window(tomorrow)
        exams = await database.prepare(
            """
            SELECT i.source_item_id, i.chat_id, i.college_id, i.course_id, i.deadline_ms,
                   g.current_semester, q.thread_id
            FROM academic_items i
            JOIN academic_groups g ON g.chat_id = i.chat_id AND g.college_id = i.college_id AND g.active = 1
            JOIN colleges col ON col.college_id = g.college_id AND col.status = 'active'
            JOIN quiz_topic_settings q ON q.chat_id = g.chat_id AND q.college_id = g.college_id
            JOIN courses c ON c.id = i.course_id AND c.chat_id = i.chat_id AND c.college_id = i.college_id AND c.active = 1
            JOIN course_offerings o ON o.course_id = c.id AND o.chat_id = c.chat_id AND o.college_id = c.college_id
                AND o.semester_label = g.current_semester AND o.active = 1
            WHERE i.item_type = 'exam' AND i.item_status IN ('active', 'verified')
              AND i.deadline_ms >= ? AND i.deadline_ms < ? AND g.current_semester IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM quiz_generation_runs r
                  WHERE r.chat_id = i.chat_id AND r.run_key = ? || i.source_item_id
              )
            ORDER BY i.deadline_ms, i.source_item_id LIMIT 1
            """
        ).bind(start_ms, end_ms, f"exam:{tomorrow.isoformat()}:").all()
        for exam in exams["results"] or []:
            if not await course_sources(database, exam["chat_id"], exam["course_id"], exam["current_semester"]):
                continue
            key = f"exam:{tomorrow.isoformat()}:{exam['source_item_id']}"
            run = await database.prepare(
                """
                INSERT INTO quiz_generation_runs (
                    college_id, chat_id, semester_label, course_id, target_thread_id,
                    run_kind, run_key, exam_item_id, status, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, 'exam', ?, ?, 'pending', ?, ?)
                ON CONFLICT(chat_id, run_key) DO NOTHING RETURNING id
                """
            ).bind(
                exam["college_id"], exam["chat_id"], exam["current_semester"], exam["course_id"],
                exam["thread_id"], key, exam["source_item_id"], now_ms, now_ms,
            ).first()
            queued += int(bool(run))
    return queued


async def claim_generation_run(database, now_ms: int) -> dict | None:
    await database.prepare(
        "UPDATE quiz_generation_runs SET status = 'failed', lease_until_ms = NULL, retry_after_ms = NULL, last_error = 'generation_attempts_exhausted', updated_at_ms = ? WHERE status = 'generating' AND lease_until_ms <= ? AND attempt_count >= 3"
    ).bind(now_ms, now_ms).run()
    return await database.prepare(
        """
        UPDATE quiz_generation_runs
        SET status = 'generating', attempt_count = attempt_count + 1,
            lease_until_ms = ?, retry_after_ms = NULL, updated_at_ms = ?
        WHERE id = (
            SELECT id FROM quiz_generation_runs
            WHERE (status = 'pending' OR (status = 'failed' AND retry_after_ms <= ?) OR
                (status = 'generating' AND lease_until_ms <= ?)) AND attempt_count < 3
              AND EXISTS (
                  SELECT 1 FROM academic_groups g JOIN colleges col ON col.college_id = g.college_id AND col.status = 'active'
                  JOIN courses c ON c.id = quiz_generation_runs.course_id AND c.chat_id = g.chat_id
                      AND c.college_id = g.college_id AND c.active = 1
                  JOIN course_offerings o ON o.chat_id = g.chat_id AND o.course_id = c.id
                      AND o.college_id = g.college_id AND o.semester_label = g.current_semester AND o.active = 1
                  WHERE g.chat_id = quiz_generation_runs.chat_id AND g.college_id = quiz_generation_runs.college_id
                    AND g.active = 1 AND g.current_semester = quiz_generation_runs.semester_label
              )
            ORDER BY created_at_ms LIMIT 1
        )
        RETURNING id, college_id, chat_id, semester_label, course_id, target_thread_id,
                  run_kind, run_key, exam_item_id, attempt_count
        """
    ).bind(now_ms + GENERATION_LEASE_MS, now_ms, now_ms, now_ms).first()


async def _ai_generated_value(ai_binding, messages: list[dict]):
    result = await ai_binding.run(AI_MODEL, {
        "messages": messages,
        "temperature": 0.2,
        "max_completion_tokens": 5000,
        "response_format": {"type": "json_object"},
    })
    if hasattr(result, "to_py"):
        result = result.to_py()
    if isinstance(result, dict):
        response = result.get("response") or result.get("result")
        if not isinstance(response, str):
            choices = result.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                response = message.get("content") if isinstance(message, dict) else choices[0].get("text")
        if isinstance(response, list):
            response = "".join(
                part.get("text", "") for part in response
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        if isinstance(response, str):
            try:
                return json.loads(response)
            except ValueError:
                return None
        return response if isinstance(response, dict) else None
    return None


async def generate_run_questions(database, ai_binding, run: dict, now_ms: int) -> bool:
    sources = await course_sources(database, run["chat_id"], run["course_id"], run["semester_label"])
    if not sources:
        await database.prepare(
            "UPDATE quiz_generation_runs SET status = 'failed', retry_after_ms = NULL, lease_until_ms = NULL, last_error = 'insufficient_source_material', updated_at_ms = ? WHERE id = ? AND status = 'generating'"
        ).bind(now_ms, run["id"]).run()
        return False
    course = await database.prepare(
        "SELECT course_name FROM courses WHERE id = ? AND chat_id = ? AND college_id = ? AND active = 1"
    ).bind(run["course_id"], run["chat_id"], run["college_id"]).first()
    if not course:
        return False
    count = 5 if run["run_kind"] == "regular" else 20
    try:
        await database.prepare(
            "DELETE FROM generated_quiz_questions WHERE run_id = ?"
        ).bind(run["id"]).run()
        await database.prepare(
            "DELETE FROM quiz_poll_deliveries WHERE run_id = ? AND status = 'pending'"
        ).bind(run["id"]).run()
        generated = await _ai_generated_value(
            ai_binding,
            build_generation_messages(course["course_name"], run["semester_label"], sources, count),
        )
        valid = validate_generated_questions(
            generated,
            count,
            {int(source["source_message_id"]) for source in sources},
            {int(source["source_message_id"]): source.get("extracted_text") or "" for source in sources},
            allow_fewer=run["run_kind"] == "exam",
        )
        if not valid:
            raise ValueError("invalid_ai_quiz_output")
        for position, question in enumerate(valid, 1):
            await database.prepare(
                """
                INSERT INTO generated_quiz_questions (
                    run_id, position, prompt, options_json, correct_option_index,
                    source_message_id, source_excerpt, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """
            ).bind(
                run["id"], position, question["prompt"], json.dumps(question["options"], ensure_ascii=False),
                question["correct_option_index"], question["source_message_id"], question["source_excerpt"], now_ms,
            ).run()
        header = f"📚 {course['course_name']} quiz — {run['semester_label']}"
        if run["run_kind"] == "exam":
            header += " · exam review"
        header += f" · {_addis_datetime(now_ms):%B %d}"
        await database.prepare(
            "INSERT INTO quiz_poll_deliveries (run_id, position, delivery_kind, payload_json, created_at_ms) VALUES (?, 0, 'header', ?, ?)"
        ).bind(run["id"], json.dumps({"chat_id": run["chat_id"], "thread_id": run["target_thread_id"], "message_text": header}), now_ms).run()
        for position, question in enumerate(valid, 1):
            poll = {
                "chat_id": run["chat_id"], "thread_id": run["target_thread_id"],
                "question": question["prompt"], "options": question["options"],
                "correct_option_id": question["correct_option_index"],
                "type": "quiz", "is_anonymous": True,
            }
            await database.prepare(
                "INSERT INTO quiz_poll_deliveries (run_id, position, delivery_kind, payload_json, created_at_ms) VALUES (?, ?, 'poll', ?, ?)"
            ).bind(run["id"], position, json.dumps(poll, ensure_ascii=False), now_ms).run()
        await database.prepare(
            "UPDATE quiz_generation_runs SET status = 'sending', lease_until_ms = NULL, last_error = NULL, updated_at_ms = ? WHERE id = ? AND status = 'generating'"
        ).bind(now_ms, run["id"]).run()
        return True
    except Exception:
        retry_after = now_ms + 5 * 60_000 if int(run["attempt_count"]) < 3 else None
        await database.prepare(
            "UPDATE quiz_generation_runs SET status = 'failed', retry_after_ms = ?, lease_until_ms = NULL, last_error = ?, updated_at_ms = ? WHERE id = ? AND status = 'generating'"
        ).bind(retry_after, "quiz_generation_failed", now_ms, run["id"]).run()
        return False


async def claim_poll_delivery(database, now_ms: int) -> dict | None:
    await database.prepare(
        "UPDATE quiz_poll_deliveries SET status = 'uncertain', last_error = 'delivery_interrupted' WHERE status = 'sending' AND attempted_at_ms <= ?"
    ).bind(now_ms - GENERATION_LEASE_MS).run()
    await database.prepare(
        """
        UPDATE quiz_generation_runs SET status = 'uncertain', updated_at_ms = ?
        WHERE status = 'sending'
          AND EXISTS (SELECT 1 FROM quiz_poll_deliveries d WHERE d.run_id = quiz_generation_runs.id AND d.status = 'uncertain')
          AND NOT EXISTS (SELECT 1 FROM quiz_poll_deliveries d WHERE d.run_id = quiz_generation_runs.id AND d.status IN ('pending', 'sending'))
        """
    ).bind(now_ms).run()
    claimed = await database.prepare(
        """
        UPDATE quiz_poll_deliveries SET status = 'sending', attempted_at_ms = ?
        WHERE id = (
            SELECT d.id FROM quiz_poll_deliveries d
            JOIN quiz_generation_runs r ON r.id = d.run_id
            JOIN academic_groups g ON g.chat_id = r.chat_id AND g.college_id = r.college_id AND g.active = 1
            JOIN colleges col ON col.college_id = r.college_id AND col.status = 'active'
            JOIN quiz_topic_settings q ON q.chat_id = r.chat_id AND q.college_id = r.college_id
                AND q.thread_id = r.target_thread_id
            JOIN course_offerings o ON o.chat_id = r.chat_id AND o.course_id = r.course_id
                AND o.college_id = r.college_id AND o.semester_label = r.semester_label AND o.active = 1
            WHERE d.status = 'pending' AND r.status = 'sending'
              AND g.current_semester = r.semester_label
            ORDER BY d.run_id, d.position LIMIT 1
        ) AND status = 'pending'
        RETURNING id, run_id, position, delivery_kind, payload_json
        """
    ).bind(now_ms).first()
    return claimed


async def finish_poll_delivery(database, delivery_id: int, now_ms: int, poll_id: str | None = None, uncertain: bool = False) -> None:
    status = "uncertain" if uncertain else "sent"
    await database.prepare(
        "UPDATE quiz_poll_deliveries SET status = ?, poll_id = ?, sent_at_ms = ?, last_error = ? WHERE id = ? AND status = 'sending'"
    ).bind(status, poll_id, now_ms, "telegram_send_uncertain" if uncertain else None, delivery_id).run()
    run = await database.prepare(
        "SELECT run_id FROM quiz_poll_deliveries WHERE id = ?"
    ).bind(delivery_id).first()
    if not run:
        return
    remaining = await database.prepare(
        "SELECT COUNT(*) AS remaining FROM quiz_poll_deliveries WHERE run_id = ? AND status IN ('pending', 'sending')"
    ).bind(run["run_id"]).first()
    uncertain_count = await database.prepare(
        "SELECT COUNT(*) AS uncertain FROM quiz_poll_deliveries WHERE run_id = ? AND status = 'uncertain'"
    ).bind(run["run_id"]).first()
    run_status = "sending" if int(remaining["remaining"]) else ("uncertain" if int(uncertain_count["uncertain"]) else "completed")
    await database.prepare(
        "UPDATE quiz_generation_runs SET status = ?, updated_at_ms = ? WHERE id = ? AND status = 'sending'"
    ).bind(run_status, now_ms, run["run_id"]).run()


async def _deliver_next_poll(database, token: str, now_ms: int, send_message, send_poll) -> str | None:
    delivery = await claim_poll_delivery(database, now_ms)
    if not delivery:
        return None
    try:
        payload = json.loads(delivery["payload_json"])
        if delivery["delivery_kind"] == "header":
            await send_message(token, payload)
            poll_id = None
        else:
            poll_id = await send_poll(token, payload)
        await finish_poll_delivery(database, delivery["id"], now_ms, poll_id=poll_id)
        return "quiz_delivery_sent"
    except Exception:
        await finish_poll_delivery(database, delivery["id"], now_ms, uncertain=True)
        return "quiz_delivery_uncertain"


async def _generate_next_quiz(database, ai_binding, now_ms: int) -> str | None:
    run = await claim_generation_run(database, now_ms)
    if not run:
        return None
    if not await consume_daily_ai_budget(database, now_ms, "generation"):
        await database.prepare(
            "UPDATE quiz_generation_runs SET status = 'pending', attempt_count = attempt_count - 1, lease_until_ms = NULL, updated_at_ms = ? WHERE id = ? AND status = 'generating'"
        ).bind(now_ms, run["id"]).run()
        return "quiz_ai_budget_reached"
    await generate_run_questions(database, ai_binding, run, now_ms)
    return "quiz_generated"


async def _extract_next_source(database, token: str, ai_binding, now_ms: int) -> str | None:
    source = await claim_extraction_source(database, now_ms)
    if not source:
        return None
    if not await consume_daily_ai_budget(database, now_ms, "extraction"):
        await database.prepare(
            "UPDATE course_quiz_sources SET extraction_state = 'pending', attempt_count = attempt_count - 1, lease_until_ms = NULL, updated_at_ms = ? WHERE id = ? AND extraction_state = 'processing'"
        ).bind(now_ms, source["id"]).run()
        return "quiz_ai_budget_reached"
    try:
        text = await extract_telegram_file_source(token, ai_binding, source)
        if source.get("caption"):
            text = f"Caption: {source['caption']}\n\n{text}"
        await finish_source_extraction(database, source["id"], now_ms, text)
        return "quiz_source_extracted"
    except ValueError as error:
        await finish_source_extraction(database, source["id"], now_ms, None, str(error))
        return "quiz_source_skipped"
    except Exception:
        await finish_source_extraction(database, source["id"], now_ms, None, "extraction_failed")
        return "quiz_source_retry"


async def process_quiz_tick(
    database, token: str, ai_binding, now_ms: int, send_message, send_poll, process_index=None
) -> str | None:
    """Do at most one AI or Telegram operation in a Cron invocation."""
    await queue_due_runs(database, now_ms)
    if process_index and await process_index(database, now_ms):
        return "tutor_source_indexed"
    for operation in (
        lambda: _deliver_next_poll(database, token, now_ms, send_message, send_poll),
        lambda: _generate_next_quiz(database, ai_binding, now_ms),
        lambda: _extract_next_source(database, token, ai_binding, now_ms),
    ):
        result = await operation()
        if result is not None:
            return result
    return None
