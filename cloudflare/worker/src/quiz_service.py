"""College-scoped quiz parsing and persistence for the Telegram Worker."""

import json
import re

QUIZ_MAX_QUESTIONS = 20


def parse_quiz_create_command(text: str) -> str | None:
    match = re.fullmatch(r"\s*/quiz_create(?:@\w+)?\s+(.+?)\s*", text, re.IGNORECASE)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title if 2 <= len(title) <= 200 else None


def parse_quiz_question_command(text: str) -> dict | None:
    parts = [part.strip() for part in text.split("|")]
    if len(parts) not in {7, 8}:
        return None
    command = re.fullmatch(r"/quiz_question(?:@\w+)?\s+(\d+)", parts[0], re.IGNORECASE)
    if not command:
        return None
    try:
        quiz_id = int(command.group(1))
        correct_option_index = int(parts[6]) - 1
    except ValueError:
        return None
    prompt = parts[1]
    options = parts[2:6]
    explanation = parts[7] if len(parts) == 8 else ""
    if (
        quiz_id <= 0
        or not 2 <= len(prompt) <= 500
        or any(not option or len(option) > 150 for option in options)
        or correct_option_index not in range(4)
        or len(explanation) > 1000
    ):
        return None
    return {
        "quiz_id": quiz_id,
        "prompt": prompt,
        "options": options,
        "correct_option_index": correct_option_index,
        "explanation": explanation or None,
    }


def parse_quiz_start_payload(payload: str) -> int | None:
    match = re.fullmatch(r"quiz_(\d{1,12})", payload.strip(), re.IGNORECASE)
    quiz_id = int(match.group(1)) if match else 0
    return quiz_id if quiz_id > 0 else None


def parse_quiz_id_command(text: str, command_name: str) -> int | None:
    if command_name not in {"quiz_publish", "quiz_results"}:
        return None
    match = re.fullmatch(rf"\s*/{command_name}(?:@\w+)?\s+(\d{{1,12}})\s*", text, re.IGNORECASE)
    quiz_id = int(match.group(1)) if match else 0
    return quiz_id if quiz_id > 0 else None


async def create_quiz(database, chat_id: str, thread_id: int, user_id: int, title: str, now_ms: int) -> int | None:
    course = await database.prepare(
        """
        SELECT g.college_id, c.id AS course_id FROM academic_groups g
        JOIN group_topics t ON t.chat_id = g.chat_id
        JOIN courses c ON c.id = t.course_id AND c.college_id = g.college_id AND c.chat_id = g.chat_id
        WHERE g.chat_id = ? AND g.active = 1 AND t.thread_id = ? AND c.active = 1
        """
    ).bind(chat_id, thread_id).first()
    if not course:
        return None
    quiz = await database.prepare(
        """
        INSERT INTO quiz_sets (college_id, chat_id, thread_id, course_id, title, created_by_user_id, created_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
        """
    ).bind(
        course["college_id"], chat_id, thread_id, course["course_id"], title.strip(), user_id, now_ms
    ).first()
    return int(quiz["id"]) if quiz else None


async def add_quiz_question(database, chat_id: str, quiz_id: int, question: dict, now_ms: int) -> int | None:
    quiz = await database.prepare(
        "SELECT id, status FROM quiz_sets WHERE id = ? AND chat_id = ? AND status = 'draft'"
    ).bind(quiz_id, chat_id).first()
    if not quiz:
        return None
    count = await database.prepare(
        "SELECT COUNT(*) AS question_count FROM quiz_questions WHERE quiz_id = ?"
    ).bind(quiz_id).first()
    position = int(count["question_count"]) + 1
    if position > QUIZ_MAX_QUESTIONS:
        return None
    saved = await database.prepare(
        """
        INSERT INTO quiz_questions (
            quiz_id, position, prompt, options_json, correct_option_index, explanation, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
        """
    ).bind(
        quiz_id,
        position,
        question["prompt"],
        json.dumps(question["options"], ensure_ascii=False),
        question["correct_option_index"],
        question.get("explanation"),
        now_ms,
    ).first()
    return int(saved["id"]) if saved else None


async def publish_quiz(database, chat_id: str, quiz_id: int, now_ms: int) -> dict | None:
    quiz = await database.prepare(
        """
        SELECT q.id, q.title, q.status, q.college_id FROM quiz_sets q
        JOIN academic_groups g ON g.chat_id = q.chat_id AND g.college_id = q.college_id AND g.active = 1
        WHERE q.id = ? AND q.chat_id = ?
        """
    ).bind(quiz_id, chat_id).first()
    if not quiz or quiz["status"] != "draft":
        return None
    count = await database.prepare(
        "SELECT COUNT(*) AS question_count FROM quiz_questions WHERE quiz_id = ?"
    ).bind(quiz_id).first()
    if not count or int(count["question_count"]) == 0:
        return None
    published = await database.prepare(
        "UPDATE quiz_sets SET status = 'published', published_at_ms = ? WHERE id = ? AND chat_id = ? AND status = 'draft' RETURNING id"
    ).bind(now_ms, quiz_id, chat_id).first()
    return quiz if published else None


async def get_published_quiz(database, quiz_id: int) -> dict | None:
    return await database.prepare(
        """
        SELECT q.id, q.college_id, q.chat_id, q.thread_id, q.course_id, q.title, c.course_name
        FROM quiz_sets q
        JOIN academic_groups g ON g.chat_id = q.chat_id AND g.college_id = q.college_id AND g.active = 1
        JOIN courses c ON c.id = q.course_id AND c.college_id = q.college_id AND c.active = 1
        WHERE q.id = ? AND q.status = 'published'
        """
    ).bind(quiz_id).first()


async def start_quiz_attempt(database, quiz_id: int, user_id: int, now_ms: int) -> int | None:
    existing = await database.prepare(
        "SELECT id FROM quiz_attempts WHERE quiz_id = ? AND user_id = ? AND status = 'in_progress'"
    ).bind(quiz_id, user_id).first()
    if existing:
        return int(existing["id"])
    attempt = await database.prepare(
        """
        INSERT INTO quiz_attempts (quiz_id, user_id, started_at_ms)
        VALUES (?, ?, ?) ON CONFLICT DO NOTHING RETURNING id
        """
    ).bind(quiz_id, user_id, now_ms).first()
    if attempt:
        return int(attempt["id"])
    existing = await database.prepare(
        "SELECT id FROM quiz_attempts WHERE quiz_id = ? AND user_id = ? AND status = 'in_progress'"
    ).bind(quiz_id, user_id).first()
    return int(existing["id"]) if existing else None


async def _next_question(database, attempt_id: int) -> dict | None:
    question = await database.prepare(
        """
        SELECT q.id, q.position, q.prompt, q.options_json, q.correct_option_index, q.explanation
        FROM quiz_attempts a JOIN quiz_questions q ON q.quiz_id = a.quiz_id
        LEFT JOIN quiz_answers ans ON ans.attempt_id = a.id AND ans.question_id = q.id
        WHERE a.id = ? AND a.status = 'in_progress' AND ans.question_id IS NULL
        ORDER BY q.position LIMIT 1
        """
    ).bind(attempt_id).first()
    if not question:
        return None
    question["options"] = json.loads(question.pop("options_json"))
    return question


async def _complete_attempt(database, attempt_id: int, now_ms: int) -> dict | None:
    score = await database.prepare(
        "SELECT COUNT(*) AS score FROM quiz_answers WHERE attempt_id = ? AND is_correct = 1"
    ).bind(attempt_id).first()
    total = await database.prepare(
        "SELECT COUNT(*) AS total FROM quiz_questions q JOIN quiz_attempts a ON a.quiz_id = q.quiz_id WHERE a.id = ?"
    ).bind(attempt_id).first()
    completed = await database.prepare(
        """
        UPDATE quiz_attempts SET status = 'completed', score = ?, total_questions = ?, completed_at_ms = ?
        WHERE id = ? AND status = 'in_progress' RETURNING score, total_questions
        """
    ).bind(int(score["score"]), int(total["total"]), now_ms, attempt_id).first()
    if completed:
        return completed
    return await database.prepare(
        "SELECT score, total_questions FROM quiz_attempts WHERE id = ? AND status = 'completed'"
    ).bind(attempt_id).first()


async def submit_quiz_answer(
    database,
    attempt_id: int,
    question_id: int,
    selected_option_index: int,
    user_id: int,
    now_ms: int,
) -> dict | None:
    attempt = await database.prepare(
        "SELECT id, quiz_id, status FROM quiz_attempts WHERE id = ? AND user_id = ?"
    ).bind(attempt_id, user_id).first()
    if not attempt:
        return None
    if attempt["status"] == "completed":
        result = await database.prepare(
            "SELECT score, total_questions FROM quiz_attempts WHERE id = ?"
        ).bind(attempt_id).first()
        return {"status": "completed", **result}

    next_question = await _next_question(database, attempt_id)
    if next_question is None:
        result = await _complete_attempt(database, attempt_id, now_ms)
        return {"status": "completed", **result} if result else None
    if next_question["id"] != question_id:
        return {"status": "in_progress", "question": next_question, "duplicate": True}
    if selected_option_index not in range(4):
        return None

    await database.prepare(
        """
        INSERT INTO quiz_answers (attempt_id, question_id, selected_option_index, is_correct, answered_at_ms)
        VALUES (?, ?, ?, ?, ?) ON CONFLICT(attempt_id, question_id) DO NOTHING
        """
    ).bind(
        attempt_id,
        question_id,
        selected_option_index,
        int(selected_option_index == next_question["correct_option_index"]),
        now_ms,
    ).run()
    following = await _next_question(database, attempt_id)
    if following:
        return {"status": "in_progress", "question": following, "duplicate": False}
    result = await _complete_attempt(database, attempt_id, now_ms)
    return {"status": "completed", **result} if result else None


async def quiz_results_summary(database, chat_id: str, quiz_id: int) -> dict | None:
    return await database.prepare(
        """
        SELECT q.title, COUNT(a.id) AS attempt_count,
               COALESCE(AVG(CASE WHEN a.status = 'completed' AND a.total_questions > 0
                   THEN 100.0 * a.score / a.total_questions END), 0) AS average_percent
        FROM quiz_sets q
        JOIN academic_groups g ON g.chat_id = q.chat_id AND g.college_id = q.college_id AND g.active = 1
        LEFT JOIN quiz_attempts a ON a.quiz_id = q.id AND a.status = 'completed'
        WHERE q.id = ? AND q.chat_id = ? AND q.status = 'published'
        GROUP BY q.id
        """
    ).bind(quiz_id, chat_id).first()
