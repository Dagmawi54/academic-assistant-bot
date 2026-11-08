# Personal Tasks and Reminders Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each student create and manage private one-time reminders and tasks through natural language and Telegram buttons, with durable D1 storage and timezone-aware delivery.

**Architecture:** Add focused task parsing, persistence, timezone, and delivery functions in a new `personal_task_service.py`. Keep Telegram update routing and callbacks in `worker.py`. Store personal tasks and reminder delivery state in separate D1 tables from academic items and the group `reminder_outbox`. Reuse the Worker's scheduled trigger and lease/retry conventions. Treat AI-parsed intent as untrusted; do not write until the action, title, and local time are validated.

**Tech Stack:** Cloudflare Python Workers, Telegram Bot API, D1, existing `AI` binding, Python `zoneinfo` with the `tzdata` package for deterministic IANA timezone support.

---

## Implementation sequence

### Task 1: Add durable personal task schema

**Files:** `cloudflare/worker/migrations/0016_personal_tasks.sql`

- [ ] Add `assistant_user_settings` with `user_id` primary key, validated-at-write `timezone_name` defaulting to `Africa/Addis_Ababa`, and `updated_at_ms`.
- [ ] Add `personal_tasks` with integer primary key, `user_id`, nullable unique `source_update_id`, bounded `title`, optional `due_at_ms`, `status` constrained to `open`, `completed`, or `cancelled`, and created/updated/completed timestamps. Index `(user_id, status, due_at_ms)`.
- [ ] Add `personal_task_reminders` with one row per task, `user_id`, `scheduled_at_ms`, `message_text`, status constrained to `pending`, `sending`, `sent`, `failed`, `cancelled`, or `uncertain`, attempt count, lease/retry/sent timestamps and bounded `last_error`; reference `personal_tasks(id)` with `ON DELETE CASCADE`. Index `(status, scheduled_at_ms, lease_until_ms, retry_after_ms)`.
- [ ] Add `assistant_pending_actions` keyed by `user_id`, storing the original update ID, validated clarification state JSON, expiry time and creation time. This supports one expiring, single-use clarification per user without partially creating a task.
- [ ] Add SQL migration tests to `cloudflare/worker/tests/test_personal_task_service.py` that apply the migration in the test database, verify constraints and indexes, and prove task rows are scoped by `user_id`.
- [ ] Run `..\..\.venv\Scripts\python.exe -m pytest tests\test_personal_task_service.py -q` from `cloudflare/worker`; expected: schema tests pass.

### Task 2: Implement timezone and intent validation

**Files:** `cloudflare/worker/pyproject.toml`, `cloudflare/worker/uv.lock`, `cloudflare/worker/src/personal_task_service.py`, `cloudflare/worker/tests/test_personal_task_service.py`

- [ ] Add `tzdata` to Worker dependencies and refresh `uv.lock` using `uv lock` from `cloudflare/worker` so IANA zone data is bundled with the Worker.
- [ ] Add `get_user_timezone(database, user_id) -> str`, returning `Africa/Addis_Ababa` when no setting exists; `set_user_timezone(database, user_id, timezone_name, now_ms) -> bool`, validating with `ZoneInfo`; and timezone-aware formatting/conversion helpers that store UTC epoch milliseconds.
- [ ] Add `TaskIntent` validation for only `create_task`, `list_tasks`, `complete_task`, `cancel_task`, `reschedule_task`, `delete_task`, `delete_all_tasks`, `clarification`, or `not_task_request`. Reject malformed JSON, missing titles, impossible/ambiguous times, past reminder times, unsupported actions, oversized text, and timezone-naive model timestamps.
- [ ] Implement `parse_task_intent(ai_binding, text, timezone_name, now_ms) -> TaskIntent` using the existing AI binding and a compact JSON-only extraction prompt. Return `not_task_request` for ordinary study questions; return `clarification` with one concise question when time/action is unclear. Never treat model output as a database instruction.
- [ ] Add tests for Addis default, valid and invalid IANA zones, UTC/local conversion across DST zones, malformed AI output, ordinary academic questions, ambiguous times, explicit dates, past dates, and oversized task titles.
- [ ] Run `..\..\.venv\Scripts\python.exe -m pytest tests\test_personal_task_service.py -q` from `cloudflare/worker`; expected: service validation tests pass.

### Task 3: Implement owner-scoped task operations and reminder delivery

**Files:** `cloudflare/worker/src/personal_task_service.py`, `cloudflare/worker/tests/test_personal_task_service.py`

- [ ] Implement `create_task(database, user_id, source_update_id, intent, timezone_name, now_ms) -> dict`; use the unique update ID for idempotency and write a reminder row only when the task has a future due/reminder instant.
- [ ] Implement `list_tasks(database, user_id, status, limit=10, cursor=None)`, `complete_task(database, user_id, task_id, now_ms)`, `cancel_task(database, user_id, task_id, now_ms)`, `reschedule_task(database, user_id, task_id, new_time_ms, now_ms)`, `delete_task(database, user_id, task_id, now_ms)`, and `delete_all_tasks(database, user_id, now_ms)`. Every mutation must include `user_id` in the SQL predicate; completion/cancellation/deletion must also cancel unsent reminders.
- [ ] Implement `claim_due_personal_task_reminders(database, now_ms, batch_size=5)` with an atomic D1 `UPDATE ... RETURNING` claim, attempt bound, and retry timestamp checks. A row left `sending` after its lease expires becomes `uncertain` rather than being resent because Telegram may already have delivered it. Implement `dispatch_claimed_personal_task_reminders(...)` so definite Telegram failures use bounded backoff and ambiguous send outcomes become `uncertain`.
- [ ] Add tests for idempotent update replay, task ownership isolation, completion/cancel/reschedule/delete behavior, no sending after cancellation, concurrent claim exclusion, lease recovery, retry exhaustion, and uncertain send outcomes.
- [ ] Run `..\..\.venv\Scripts\python.exe -m pytest tests\test_personal_task_service.py -q` from `cloudflare/worker`; expected: CRUD and delivery tests pass.

### Task 4: Route private task requests and add Telegram controls

**Files:** `cloudflare/worker/src/worker.py`, `cloudflare/worker/tests/worker_loader.py`, `cloudflare/worker/tests/test_worker.py`, `cloudflare/worker/tests/test_personal_task_service.py`

- [ ] Route only private-chat task intents through `parse_task_intent` before the general tutor; ordinary questions continue through the current tutor unchanged. Reject group task requests with a short prompt to use private chat.
- [ ] Add `personal_task_service` to the sibling-service stubs in `worker_loader.py` so the existing Worker test loader remains deterministic while the production Worker imports the real service module.
- [ ] Add `/tasks`, `/timezone`, and `/forget_tasks` command handlers. `/timezone` accepts a validated IANA name and reports the active zone. `/forget_tasks` requires a confirmation callback before deleting all owner-scoped tasks.
- [ ] Add private `home:tasks` and `home:add_reminder` menu actions. Task list rows use inline buttons for complete, edit title, reschedule, cancel, and delete; callbacks must verify `callback.from.id` matches the stored owner, answer the callback, and give a concise result.
- [ ] Use ForceReply only when collecting a missing task title/time or an edit value; persist no partially specified task while waiting for clarification. Bind pending clarification state to the user and original update in D1, expire it, and consume it once.
- [ ] Format dates in the user's timezone and escape all dynamic Telegram HTML. Keep callback payloads short and validate task IDs before database use.
- [ ] Add Worker routing/callback tests for task intent vs study question, private-only behavior, callback owner mismatch, timezone command, clarification continuation, menu buttons, and Telegram response formatting.
- [ ] From the repository root, run `.venv\Scripts\python.exe -m pytest cloudflare\worker\tests\test_worker.py -k "task or timezone" -q`; from `cloudflare/worker`, run `..\..\.venv\Scripts\python.exe -m pytest tests\test_personal_task_service.py -q`. Expected: all targeted tests pass.

### Task 5: Wire scheduled delivery and verify deployment artifact

**Files:** `cloudflare/worker/src/worker.py`, `cloudflare/worker/tests/test_worker.py`, `cloudflare/worker/README.md`

- [ ] Call `claim_due_personal_task_reminders` and `dispatch_claimed_personal_task_reminders` on the existing minute scheduled tick, independently of group `reminder_outbox` dispatch. Use the private `user_id` as Telegram `chat_id`; never include group IDs or course data in personal reminder records.
- [ ] Preserve existing digest, course quiz, group reminder and cleanup ordering. Add failure isolation so a personal reminder query/send error does not skip the other scheduled jobs, and log a stable error code without task text or user secrets.
- [ ] Add scheduled-handler tests showing both group and personal reminders are processed, each failure path remains isolated, and private sends target only the owner.
- [ ] Document the new commands, natural-language examples, timezone behavior, task privacy, ambiguity clarification and D1 migration command in `cloudflare/worker/README.md`.
- [ ] From the repository root, run `.venv\Scripts\python.exe -m pytest cloudflare\worker\tests\test_worker.py -k "task or reminder" -q`; from `cloudflare/worker`, run `..\..\.venv\Scripts\python.exe -m pytest tests\test_personal_task_service.py -q`.
- [ ] From `cloudflare/worker`, run `npx wrangler deploy --dry-run`; expected: Worker bundle builds and migration metadata is recognized. Do not apply the D1 migration to production or deploy without the user's explicit deployment request.

## Completion checklist

- [ ] A student can create, list, complete, cancel, reschedule and delete only their own tasks.
- [ ] Clear one-time reminder requests persist durably and deliver in the requested timezone; ambiguous requests ask before writing.
- [ ] Replayed Telegram updates and overlapping scheduler ticks do not create duplicate tasks or normal duplicate sends.
- [ ] Existing class reminders, tutor memory, course search, quizzes and file analysis remain unchanged and their targeted regression tests pass.
- [ ] The Worker dry-run bundle succeeds; production migration/deployment is handled as a separate explicit rollout step.
