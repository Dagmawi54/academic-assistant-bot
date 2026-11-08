"""Conservative, auditable setup helpers for Telegram college groups."""

from __future__ import annotations

import course_quiz_service

MAX_COURSE_CHOICES = 5


def normalize_topic_title(title: str) -> str:
    title = str(title or "").strip()
    if not 1 <= len(title) <= 255 or any(ord(char) < 32 for char in title):
        raise ValueError("invalid_topic_title")
    return course_quiz_service.normalize_course_name(title)


def classify_topic_matches(topics: list[dict], offerings: list[dict]) -> list[dict]:
    """Auto-apply only one exact active course-name match with no conflicting mapping."""
    active = [
        offering for offering in offerings
        if offering.get("active", 1) and type(offering.get("course_id")) is int
    ]
    ordered_offerings = sorted(
        active,
        key=lambda row: (str(row.get("course_name") or "").casefold(), int(row["course_id"])),
    )
    decisions = []
    for topic in topics:
        title = str(topic.get("topic_title") or "")
        try:
            normalized = normalize_topic_title(title)
        except ValueError:
            normalized = ""
        exact = [
            row for row in ordered_offerings
            if normalized and course_quiz_service.normalize_course_name(
                str(row.get("course_name") or "")
            ) == normalized
        ]
        current = topic.get("current_course_id")
        if len(exact) == 1 and current is None:
            decisions.append({
                "thread_id": int(topic["thread_id"]),
                "topic_title": title,
                "status": "auto_apply",
                "course_id": int(exact[0]["course_id"]),
                "course_name": str(exact[0]["course_name"]),
                "candidates": [dict(exact[0])],
            })
        elif len(exact) == 1 and int(current) == int(exact[0]["course_id"]):
            decisions.append({
                "thread_id": int(topic["thread_id"]),
                "topic_title": title,
                "status": "already_mapped",
                "course_id": int(current),
                "course_name": str(exact[0]["course_name"]),
                "candidates": [dict(exact[0])],
            })
        else:
            choices = exact if exact else ordered_offerings
            decisions.append({
                "thread_id": int(topic["thread_id"]),
                "topic_title": title,
                "status": "needs_admin_choice",
                "course_id": None,
                "course_name": None,
                "candidates": [dict(row) for row in choices[:MAX_COURSE_CHOICES]],
            })
    return decisions


async def _known_topics(database, chat_id: str) -> list[dict]:
    mapped = await database.prepare(
        """
        SELECT t.thread_id, COALESCE(o.topic_title, c.course_name) AS topic_title,
               t.course_id AS current_course_id, c.course_name AS mapped_course_name
        FROM group_topics t
        JOIN courses c ON c.id = t.course_id
        LEFT JOIN observed_group_topics o ON o.chat_id = t.chat_id AND o.thread_id = t.thread_id
        WHERE t.chat_id = ?
        ORDER BY t.thread_id
        """
    ).bind(chat_id).all()
    observed = await database.prepare(
        """
        SELECT o.thread_id, o.topic_title, NULL AS current_course_id,
               NULL AS mapped_course_name
        FROM observed_group_topics o
        LEFT JOIN group_topics t ON t.chat_id = o.chat_id AND t.thread_id = o.thread_id
        WHERE o.chat_id = ? AND t.thread_id IS NULL
        ORDER BY o.observed_at_ms, o.thread_id
        """
    ).bind(chat_id).all()
    result = list((mapped or {}).get("results") or [])
    result.extend((observed or {}).get("results") or [])
    return result


async def get_group_setup_state(database, chat_id: str) -> dict:
    group = await database.prepare(
        "SELECT college_id, group_title, current_semester FROM academic_groups WHERE chat_id = ? AND active = 1"
    ).bind(chat_id).first()
    if not group:
        return {
            "status": "unlinked", "chat_id": chat_id, "college_id": None,
            "group_title": None, "semester_label": None, "offerings": [], "topics": [],
            "unmapped_offerings": [],
        }
    topics = await _known_topics(database, chat_id)
    semester = str(group.get("current_semester") or "").strip()
    if not semester:
        return {
            "status": "semester_required", "chat_id": chat_id,
            "college_id": group["college_id"], "group_title": group.get("group_title"),
            "semester_label": None, "offerings": [], "topics": topics,
            "unmapped_offerings": [],
        }
    result = await database.prepare(
        """
        SELECT co.course_id, c.course_name, c.normalized_name, co.semester_label, co.active
        FROM course_offerings co
        JOIN courses c ON c.id = co.course_id AND c.college_id = co.college_id AND c.chat_id = co.chat_id
        WHERE co.college_id = ? AND co.chat_id = ? AND co.semester_label = ?
          AND co.active = 1 AND c.active = 1
        ORDER BY c.course_name COLLATE NOCASE, co.course_id
        """
    ).bind(group["college_id"], chat_id, semester).all()
    offerings = list((result or {}).get("results") or [])
    mapped_ids = {
        int(topic["current_course_id"])
        for topic in topics if topic.get("current_course_id") is not None
    }
    return {
        "status": "ready", "chat_id": chat_id, "college_id": group["college_id"],
        "group_title": group.get("group_title"), "semester_label": semester,
        "offerings": offerings, "topics": topics,
        "unmapped_offerings": [row for row in offerings if int(row["course_id"]) not in mapped_ids],
    }


async def observe_forum_topic(
    database, chat_id: str, thread_id: int, topic_title: str,
    observed_by_user_id: int | None, now_ms: int,
) -> bool:
    try:
        normalize_topic_title(topic_title)
    except ValueError:
        return False
    if not isinstance(chat_id, str) or not chat_id or type(thread_id) is not int or thread_id <= 0:
        return False
    row = await database.prepare(
        """
        INSERT INTO observed_group_topics
            (chat_id, thread_id, topic_title, observed_by_user_id, observed_at_ms)
        SELECT chat_id, ?, ?, ?, ? FROM academic_groups
        WHERE chat_id = ? AND active = 1
        ON CONFLICT(chat_id, thread_id) DO UPDATE SET
            topic_title = excluded.topic_title,
            observed_by_user_id = excluded.observed_by_user_id,
            observed_at_ms = excluded.observed_at_ms
        RETURNING thread_id
        """
    ).bind(thread_id, topic_title.strip(), observed_by_user_id, now_ms, chat_id).first()
    return bool(row)


async def begin_setup_run(database, chat_id: str, user_id: int, now_ms: int) -> dict:
    state = await get_group_setup_state(database, chat_id)
    state["run_id"] = None
    state["decisions"] = []
    if state["status"] != "ready":
        return state
    row = await database.prepare(
        """
        INSERT INTO group_setup_runs
            (chat_id, college_id, semester_label, started_by_user_id, status, created_at_ms)
        VALUES (?, ?, ?, ?, 'reviewing', ?)
        RETURNING id
        """
    ).bind(chat_id, state["college_id"], state["semester_label"], user_id, now_ms).first()
    if not row:
        return {**state, "status": "unavailable"}
    state["run_id"] = int(row["id"])
    state["decisions"] = classify_topic_matches(state["topics"], state["offerings"])
    return state


async def apply_exact_matches(
    database, run_id: int, user_id: int, matches: list[dict], now_ms: int,
) -> dict:
    run = await database.prepare(
        "SELECT id, chat_id, college_id, semester_label, status FROM group_setup_runs WHERE id = ?"
    ).bind(run_id).first()
    if not run or run.get("status") not in {"reviewing", "completed"}:
        return {"status": "stale", "applied_count": 0}
    state = await get_group_setup_state(database, str(run["chat_id"]))
    if state.get("status") != "ready" or state.get("college_id") != run["college_id"] or state.get("semester_label") != run["semester_label"]:
        await database.prepare(
            "UPDATE group_setup_runs SET status = 'stale', completed_at_ms = ? WHERE id = ?"
        ).bind(now_ms, run_id).run()
        return {"status": "stale", "applied_count": 0}
    current_decisions = classify_topic_matches(state["topics"], state["offerings"])
    allowed = {
        (int(item["thread_id"]), int(item["course_id"]))
        for item in current_decisions if item["status"] == "auto_apply"
    }
    topic_by_id = {int(topic["thread_id"]): topic for topic in state["topics"]}
    applied_count = 0
    for proposed in matches:
        try:
            thread_id = int(proposed["thread_id"])
            course_id = int(proposed["course_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if (thread_id, course_id) not in allowed:
            continue
        topic = topic_by_id.get(thread_id)
        if not topic or topic.get("current_course_id") is not None:
            continue
        existing = await database.prepare(
            "SELECT id FROM group_setup_changes WHERE run_id = ? AND thread_id = ?"
        ).bind(run_id, thread_id).first()
        if existing:
            continue
        group = await database.prepare(
            "SELECT college_id, current_semester FROM academic_groups WHERE chat_id = ? AND active = 1"
        ).bind(run["chat_id"]).first()
        if not group or group.get("college_id") != run["college_id"] or group.get("current_semester") != run["semester_label"]:
            await database.prepare(
                "UPDATE group_setup_runs SET status = 'stale', completed_at_ms = ? WHERE id = ?"
            ).bind(now_ms, run_id).run()
            return {"status": "stale", "applied_count": applied_count}
        await database.batch([
            database.prepare(
                "INSERT INTO group_topics (chat_id, thread_id, course_id, created_by_user_id, created_at_ms) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(chat_id, thread_id) DO UPDATE SET "
                "course_id = excluded.course_id, created_by_user_id = excluded.created_by_user_id, created_at_ms = excluded.created_at_ms"
            ).bind(run["chat_id"], thread_id, course_id, user_id, now_ms),
            database.prepare(
                "INSERT INTO group_setup_changes (run_id, chat_id, thread_id, previous_course_id, applied_course_id, applied_by_user_id, created_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)"
            ).bind(run_id, run["chat_id"], thread_id, topic.get("current_course_id"), course_id, user_id, now_ms),
        ])
        applied_count += 1
    await database.prepare(
        "UPDATE group_setup_runs SET status = 'completed', completed_at_ms = ? WHERE id = ? AND status IN ('reviewing', 'completed')"
    ).bind(now_ms, run_id).run()
    return {"status": "completed", "applied_count": applied_count}


async def resolve_topic_match(
    database, run_id: int, thread_id: int, selected_course_id: int, user_id: int, now_ms: int,
) -> bool:
    run = await database.prepare(
        "SELECT id, chat_id, college_id, semester_label, status FROM group_setup_runs WHERE id = ?"
    ).bind(run_id).first()
    if not run or run.get("status") not in {"reviewing", "completed"}:
        return False
    group = await database.prepare(
        "SELECT college_id, current_semester FROM academic_groups WHERE chat_id = ? AND active = 1"
    ).bind(run["chat_id"]).first()
    if not group or group.get("college_id") != run["college_id"] or group.get("current_semester") != run["semester_label"]:
        await database.prepare(
            "UPDATE group_setup_runs SET status = 'stale', completed_at_ms = ? WHERE id = ?"
        ).bind(now_ms, run_id).run()
        return False
    topic = await database.prepare(
        """
        SELECT thread_id, course_id AS current_course_id FROM group_topics
        WHERE chat_id = ? AND thread_id = ?
        UNION ALL
        SELECT o.thread_id, NULL AS current_course_id FROM observed_group_topics o
        WHERE o.chat_id = ? AND o.thread_id = ?
          AND NOT EXISTS (SELECT 1 FROM group_topics t WHERE t.chat_id = o.chat_id AND t.thread_id = o.thread_id)
        LIMIT 1
        """
    ).bind(run["chat_id"], thread_id, run["chat_id"], thread_id).first()
    if not topic:
        return False
    offering = await database.prepare(
        """
        SELECT c.id FROM courses c JOIN course_offerings o
          ON o.course_id = c.id AND o.chat_id = c.chat_id AND o.college_id = c.college_id
        WHERE c.id = ? AND c.chat_id = ? AND c.college_id = ? AND c.active = 1
          AND o.semester_label = ? AND o.active = 1
        """
    ).bind(selected_course_id, run["chat_id"], run["college_id"], run["semester_label"]).first()
    if not offering or (topic.get("current_course_id") is not None and int(topic["current_course_id"]) == selected_course_id):
        return False
    existing = await database.prepare(
        "SELECT id FROM group_setup_changes WHERE run_id = ? AND thread_id = ?"
    ).bind(run_id, thread_id).first()
    if existing:
        return False
    await database.batch([
        database.prepare(
            "INSERT INTO group_topics (chat_id, thread_id, course_id, created_by_user_id, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(chat_id, thread_id) DO UPDATE SET "
            "course_id = excluded.course_id, created_by_user_id = excluded.created_by_user_id, created_at_ms = excluded.created_at_ms"
        ).bind(run["chat_id"], thread_id, selected_course_id, user_id, now_ms),
        database.prepare(
            "INSERT INTO group_setup_changes (run_id, chat_id, thread_id, previous_course_id, applied_course_id, applied_by_user_id, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        ).bind(run_id, run["chat_id"], thread_id, topic.get("current_course_id"), selected_course_id, user_id, now_ms),
        database.prepare(
            "UPDATE group_setup_runs SET status = 'completed', completed_at_ms = ? WHERE id = ?"
        ).bind(now_ms, run_id),
    ])
    return True


async def undo_latest_setup_run(database, chat_id: str, user_id: int, now_ms: int) -> dict:
    run = await database.prepare(
        "SELECT id, status FROM group_setup_runs WHERE chat_id = ? ORDER BY id DESC LIMIT 1"
    ).bind(chat_id).first()
    if not run:
        return {"status": "not_found", "undone_count": 0}
    if run.get("status") == "undone":
        return {"status": "already_undone", "undone_count": 0}
    changes = await database.prepare(
        "SELECT id, thread_id, previous_course_id, applied_course_id FROM group_setup_changes WHERE run_id = ? AND undone_at_ms IS NULL ORDER BY id"
    ).bind(run["id"]).all()
    rows = list((changes or {}).get("results") or [])
    for change in rows:
        current = await database.prepare(
            "SELECT course_id FROM group_topics WHERE chat_id = ? AND thread_id = ?"
        ).bind(chat_id, change["thread_id"]).first()
        if not current or int(current["course_id"]) != int(change["applied_course_id"]):
            await database.prepare(
                "UPDATE group_setup_runs SET status = 'stale', completed_at_ms = ? WHERE id = ?"
            ).bind(now_ms, run["id"]).run()
            return {"status": "stale", "undone_count": 0}
    undone_count = 0
    for change in rows:
        if change.get("previous_course_id") is None:
            mutation = database.prepare(
                "DELETE FROM group_topics WHERE chat_id = ? AND thread_id = ? AND course_id = ?"
            ).bind(chat_id, change["thread_id"], change["applied_course_id"])
        else:
            mutation = database.prepare(
                "UPDATE group_topics SET course_id = ?, created_by_user_id = ?, created_at_ms = ? "
                "WHERE chat_id = ? AND thread_id = ? AND course_id = ?"
            ).bind(change["previous_course_id"], user_id, now_ms, chat_id, change["thread_id"], change["applied_course_id"])
        await database.batch([
            mutation,
            database.prepare(
                "UPDATE group_setup_changes SET undone_at_ms = ? WHERE id = ? AND undone_at_ms IS NULL AND changes() = 1"
            ).bind(now_ms, change["id"]),
        ])
        recorded = await database.prepare(
            "SELECT undone_at_ms FROM group_setup_changes WHERE id = ?"
        ).bind(change["id"]).first()
        if recorded and recorded.get("undone_at_ms") is not None:
            undone_count += 1
        else:
            await database.prepare(
                "UPDATE group_setup_runs SET status = 'stale', completed_at_ms = ? WHERE id = ?"
            ).bind(now_ms, run["id"]).run()
            return {"status": "stale", "undone_count": undone_count}
    await database.prepare(
        "UPDATE group_setup_runs SET status = 'undone', completed_at_ms = ? WHERE id = ?"
    ).bind(now_ms, run["id"]).run()
    return {"status": "undone", "undone_count": undone_count}
