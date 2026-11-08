# Guided College Setup Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give authorized college group admins a guided view of semester/course/topic setup, safely auto-map only exact matches, ask admins to resolve uncertain matches, and provide a guarded undo for the latest setup run.

**Architecture:** Add a focused `group_setup_service.py` for D1 reads, exact-match classification, run/change journaling and guarded undo. Keep Telegram update routing and inline callback handling in `worker.py`. Reuse the college/group/course/offering schema, invite flow and current Telegram admin/representative authorization. Learn new topic names only from bot-visible `forum_topic_created` updates and existing D1 state; never use a user-account session or claim that the bot can enumerate old topics.

**Tech Stack:** Cloudflare Python Workers, Telegram Bot API, D1, current `course_quiz_service` course normalization and offering queries.

---

## Implementation sequence

### Task 1: Store observed topic titles and setup audit history

**Files:** `cloudflare/worker/migrations/0017_guided_group_setup.sql`, `cloudflare/worker/tests/test_group_setup_service.py`

- [ ] Add `observed_group_topics(chat_id, thread_id, topic_title, observed_by_user_id NULL, observed_at_ms)` with primary key `(chat_id, thread_id)` and foreign key to `academic_groups(chat_id)` with cascade delete. Use a 255-character title constraint and an index by `(chat_id, observed_at_ms)`.
- [ ] Add `group_setup_runs(id, chat_id, college_id, semester_label, started_by_user_id, status, created_at_ms, completed_at_ms)` with status constrained to `reviewing`, `completed`, `undone`, or `stale`.
- [ ] Add `group_setup_changes(id, run_id, chat_id, thread_id, previous_course_id, applied_course_id, applied_by_user_id, created_at_ms, undone_at_ms)` with foreign keys and one change per `(run_id, thread_id)`. Preserve `previous_course_id IS NULL` to distinguish new mappings from remaps.
- [ ] Add migration tests that apply all schema migrations in order and verify topic uniqueness, run/change references, and nullable prior mapping behavior.
- [ ] Run `..\..\.venv\Scripts\python.exe -m pytest tests\test_group_setup_service.py -q` from `cloudflare/worker`; expected: schema tests pass.

### Task 2: Implement setup state and conservative match classification

**Files:** `cloudflare/worker/src/group_setup_service.py`, `cloudflare/worker/tests/test_group_setup_service.py`

- [ ] Implement `get_group_setup_state(database, chat_id) -> dict` returning the active college, current semester, active offerings, mapped topic titles/courses, observed unmapped topics and course offerings not yet mapped. Return a structured `semester_required` state when `academic_groups.current_semester` is unset.
- [ ] Implement `normalize_topic_title(title)` by reusing `course_quiz_service.normalize_course_name` where equivalent; reject empty/control-character titles and bound saved titles to 255 characters.
- [ ] Implement `classify_topic_matches(topics, offerings)` so auto-apply is possible only when normalized full topic title equals exactly one normalized course name in the active semester, and the candidate is not already mapped to a different course. The current schema has no separate course-code field, so do not infer one. Return all other cases as `needs_admin_choice` with a bounded, deterministic candidate list; never use AI or fuzzy similarity to auto-apply.
- [ ] Implement `begin_setup_run(database, chat_id, user_id, now_ms)` after caller authorization, capturing current semester and a stable run ID. If the semester is missing, return the state without starting an apply run.
- [ ] Add tests for exact normalized title match, exact course-code match, ambiguous duplicate offerings, partial/fuzzy title, inactive offerings, already-mapped topic, missing semester, missing college link and unseen old topic behavior.
- [ ] Run `..\..\.venv\Scripts\python.exe -m pytest tests\test_group_setup_service.py -q` from `cloudflare/worker`; expected: classification and state tests pass.

### Task 3: Apply exact matches and implement stale-safe undo

**Files:** `cloudflare/worker/src/group_setup_service.py`, `cloudflare/worker/tests/test_group_setup_service.py`

- [ ] Implement `apply_exact_matches(database, run_id, user_id, matches, now_ms)` using one D1 `batch()` transaction per topic change. For each topic, capture the prior `group_topics.course_id`, write the resulting mapping, activate its `course_offerings` row for the run semester, and write the audit record as one atomic operation.
- [ ] Ensure a run cannot apply across a changed current semester: re-read the active group and semester immediately before each mapping write and require them to match the run snapshot. Record only successful mapping changes.
- [ ] Implement `resolve_topic_match(database, run_id, thread_id, selected_course_id, user_id, now_ms)`. Verify selected course is an active offering for the run's college, group and semester; record the previous mapping and map only that topic in one D1 `batch()` transaction.
- [ ] Implement `undo_latest_setup_run(database, chat_id, user_id, now_ms)`. Only the initiating/authorized group admin or representative may undo. For each journal row, compare the current topic mapping with `applied_course_id`; if a newer change exists, mark the run stale and do not overwrite it. Otherwise restore `previous_course_id` or delete the newly created mapping in one D1 `batch()` transaction with its audit update.
- [ ] Add tests for new mapping, remapping, selected candidate validation, semester changed mid-run, unauthorized caller rejection at Worker boundary, latest-run selection, restoring prior mappings, deleting new mappings, and refusing stale overwrite.
- [ ] Run `..\..\.venv\Scripts\python.exe -m pytest tests\test_group_setup_service.py -q` from `cloudflare/worker`; expected: apply/undo tests pass.

### Task 4: Capture Telegram topic-created events and route setup controls

**Files:** `cloudflare/worker/src/worker.py`, `cloudflare/worker/tests/worker_loader.py`, `cloudflare/worker/tests/test_worker.py`, `cloudflare/worker/tests/test_group_setup_service.py`

- [ ] In `_process_telegram_update`, detect message service payload `forum_topic_created` in an active registered group. Store its `message_thread_id` and title with an idempotent upsert. Do not infer a thread ID if absent; log a stable skip reason and continue normal update handling.
- [ ] Add `group_setup_service` to the sibling-service stubs in `worker_loader.py`; keep production imports bound to the real service module.
- [ ] Add `/group_setup` for a group admin or registered representative. It must report college, active semester, mapped/observed/unmapped counts, exact matches ready to apply and uncertain matches requiring selection. Explain that historical topics never seen by the bot must be mapped by opening that topic and using the existing `/course_map <course name>` flow.
- [ ] If the current semester is missing, provide an inline “Set semester” action and preserve `/semester_set` as the write command; accept the semester label from the admin and refresh setup state before applying mappings.
- [ ] Add compact callback actions under a `setup:` namespace for preview, apply exact matches, choose a course for an uncertain topic, and undo latest run. Recheck admin/representative authorization on every state-changing callback; do not trust callback data alone.
- [ ] Ensure candidate buttons are limited to offerings from the selected current semester, respect Telegram callback data size limits, and handle expired/stale run IDs with a fresh-state response.
- [ ] Add Worker tests for event storage, missing `message_thread_id`, duplicate update handling, private/group routing, permission denial, missing semester, exact apply confirmation, uncertain candidate selection, callback expiration and topic mapping help text.
- [ ] From the repository root, run `.venv\Scripts\python.exe -m pytest cloudflare\worker\tests\test_worker.py -k "group_setup or forum_topic_created or course_map" -q`; from `cloudflare/worker`, run `..\..\.venv\Scripts\python.exe -m pytest tests\test_group_setup_service.py -q`. Expected: targeted tests pass.

### Task 5: Document operator and admin flow; validate Worker bundle

**Files:** `cloudflare/worker/README.md`, `cloudflare/worker/src/worker.py`, `cloudflare/worker/tests/test_worker.py`

- [ ] Document admin prerequisites, invite linking, setting the active semester, starting guided setup, exact vs uncertain match behavior, mapping older unseen topics, undo limits, and the fact that Bot API does not enumerate historical forum topics.
- [ ] Add an end-to-end Worker test using a D1 test double: register/link group, set semester, observe a new topic-created event, preview, apply one exact match, select a candidate for a second topic, and undo the most recent run.
- [ ] From the repository root, run `.venv\Scripts\python.exe -m pytest cloudflare\worker\tests\test_worker.py -k "group_setup or forum_topic_created" -q`; from `cloudflare/worker`, run `..\..\.venv\Scripts\python.exe -m pytest tests\test_group_setup_service.py -q`.
- [ ] From `cloudflare/worker`, run `npx wrangler deploy --dry-run`; expected: Worker bundle builds and the new migration is discovered. Do not apply the production migration or deploy without an explicit deployment request.

## Completion checklist

- [ ] Setup works only for registered college groups and authorized admins/representatives.
- [ ] Exact matches are deterministic and semester-scoped; uncertain matches always wait for an admin choice.
- [ ] New Telegram topic-created updates are stored idempotently; unseen historical topics get a clear manual path.
- [ ] Every automatic or manual setup mapping is auditable and the latest run can be undone only when no newer mapping has replaced it.
- [ ] Existing invite linking, `/semester_set`, `/course_map`, quiz-topic mapping, course searches, reminders and file analysis continue to work.
