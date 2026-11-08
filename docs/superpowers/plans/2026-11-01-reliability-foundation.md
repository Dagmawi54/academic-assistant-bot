# Academic Assistant Reliability Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing Python Telegram bot reliably persist academic events and reminders, safely process retried updates, and refuse ephemeral databases in deployed environments.

**Architecture:** The target is a Cloudflare Python Worker using FastAPI, a D1 binding adapter, a Workers-compatible Telegram Bot API transport, and a Cron scheduled handler. D1 is the durable source of truth; Worker local files and in-memory scheduler state are never persistent storage. Retain the current FastAPI + aiogram runtime for local development while progressively replacing unsupported infrastructure behind narrow interfaces. Validate each package and boundary in a local Worker proof before porting that feature. The free tier has a 10 ms CPU budget per HTTP and Cron invocation, so measured workload compatibility is a release gate.

**Tech Stack:** Python 3.11, FastAPI, aiogram, SQLAlchemy 2 async, Alembic, APScheduler, PostgreSQL, pytest/pytest-asyncio.

---

## Scope and checkpoint boundaries

This plan implements delivery checkpoint 1 from `docs/superpowers/specs/2026-11-01-telegram-college-academic-assistant-design.md`: database/runtime reliability, assignment and exam capture, update idempotency, durable reminders, and recovery diagnostics. It does not add resource search, quizzes, polls, or the Mini App. Those are separate plans after this checkpoint is stable.

Cloudflare is the selected target. The current code cannot use D1 through SQLAlchemy as-is, and aiogram may not use its normal HTTP client in Workers. Add a platform-specific D1 repository and Telegram `fetch` transport where required. Do not claim a successful Cloudflare deployment until the Worker build, D1 read/write, webhook request, scheduled invocation, and CPU budget are demonstrated. If any critical feature cannot fit free Worker limits, keep Cloudflare and use a paid Workers plan or move only the scheduled/compute component to a Python host with hosted PostgreSQL; never deploy with app-local SQLite.

## Project map

- `app/config.py`: environment parsing, database URL normalization, and production guardrails.
- `app/database/models.py`: SQLAlchemy schema for group, course, academic item, and reminder records.
- `app/database/session.py`: engine/session lifecycle and database readiness probe.
- `app/database/crud.py`: shared query helpers, including pending/due reminder reads.
- `app/services/routing_service.py`: academic classification, extraction, duplicate suppression, and item creation.
- `app/services/reminder_service.py`: reminder row creation and cancellation.
- `app/reminders/scheduler.py`: startup reconciliation, due dispatch, and Telegram delivery.
- `app/bot/handlers/group.py`: Telegram message intake and source context.
- `app/bot/webhook.py`: webhook validation and liveness/readiness endpoints.
- `alembic.ini`, `migrations/`: versioned schema upgrades for deployed databases.
- `tests/conftest.py`, `tests/test_services.py`, and new focused test modules: isolated async tests and regression coverage.
- `docs/live-validation/academic-os-phase-1.md`: test-group checklist; update this after runtime behavior is verified.

## Task 1: Establish the current failure baseline

**Files:**
- Read: `tests/conftest.py`
- Read: `tests/test_services.py`
- Read: `tests/test_acceptance_diagnostics.py`
- Read: `app/services/routing_service.py`
- Read: `app/reminders/scheduler.py`

- [ ] **Step 1: Run the existing suite and record failures**

Run: `python -m pytest -q`

Expected: pytest reports a final passed/failed count. Record every failure before changing behavior; do not hide unrelated existing failures.

- [ ] **Step 2: Run lint on the affected modules**

Run: `python -m ruff check app/config.py app/database app/services/routing_service.py app/services/reminder_service.py app/reminders/scheduler.py app/bot/handlers/group.py app/bot/webhook.py tests`

Expected: a concrete list of lint findings that can be fixed in the affected files; retain unrelated findings in the work notes.

## Task 2: Make production database configuration explicit

**Files:**
- Modify: `app/config.py`
- Modify: `app/database/session.py`
- Modify: `render.yaml`
- Modify: `.env.example`
- Create: `tests/test_database_configuration.py`

- [ ] **Step 1: Add configuration tests for local and deployed modes**

Test cases must assert that a local settings object with `sqlite+aiosqlite:///./bot.db` is accepted in development; deployed mode with a SQLite URL raises a clear configuration error; deployed mode with a PostgreSQL URL is accepted; and a missing deployed `DATABASE_URL` fails before the engine is created.

Run: `python -m pytest tests/test_database_configuration.py -q`

Expected before implementation: the new tests fail because there is no deployment-mode database guard.

- [ ] **Step 2: Add an explicit environment mode and validate the database URL**

Implement a `deployment_environment` setting defaulting to `development`. In `production`, accept PostgreSQL URLs only; reject SQLite with an error naming `DATABASE_URL` and the configured environment. Keep the default local SQLite URL for developers and tests. Validate before module-level engine creation so a bad deployment does not silently create `bot.db`.

- [ ] **Step 3: Make connection pooling configurable for hosted PostgreSQL**

Read `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` from settings, preserve current defaults for production, and use SQLite-compatible engine options when explicitly running locally. Do not print the database URL or credentials in logs.

- [ ] **Step 4: Update deployment examples**

Change `.env.example` to show `APP_ENV=development` and a local-only SQLite URL. Change `render.yaml` comments and variables to require a hosted PostgreSQL `DATABASE_URL` and `APP_ENV=production`; do not declare a free ephemeral SQLite fallback as deployable storage.

- [ ] **Step 5: Verify the configuration cases**

Run: `python -m pytest tests/test_database_configuration.py -q`

Expected: all cases pass and no real `.env` secret is printed.

## Task 3: Version database schema changes

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/0001_initial_schema.py`
- Modify: `app/database/session.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_migrations.py`

- [ ] **Step 1: Add a migration smoke test against an isolated SQLite file**

The test invokes Alembic upgrade to head on a temporary test path and asserts that `groups`, `academic_items`, and `reminders` exist. It must not point at `.env`, `bot.db`, `app.db`, or `academic_bot.db`.

Run: `python -m pytest tests/test_migrations.py -q`

Expected before implementation: collection or execution fails because Alembic configuration and migration scripts do not exist.

- [ ] **Step 2: Create the initial migration from the current SQLAlchemy metadata**

Configure Alembic to use `Base.metadata`, honor `DATABASE_URL`, and support async application URLs through Alembic's async engine pattern. Generate the baseline schema from the current models; do not add unrelated schema changes in the baseline.

- [ ] **Step 3: Separate local table creation from production startup**

Keep `Base.metadata.create_all` for development/test bootstrap only. Production startup must run a migration check or require the release command `alembic upgrade head`; it must not attempt ad hoc optional `ALTER TABLE` statements and silently continue when a migration fails.

- [ ] **Step 4: Verify migration upgrade**

Run: `python -m pytest tests/test_migrations.py -q`

Expected: the isolated schema contains all current tables and indexes; no workspace database file changes.

## Task 4: Add college ownership boundaries

**Files:**
- Modify: `app/database/models.py`
- Modify: `app/database/crud.py`
- Modify: `app/admin/permissions.py`
- Create: `migrations/versions/0002_college_scope.py`
- Create: `tests/test_college_isolation.py`

- [ ] **Step 1: Add cross-college access regression tests**

Create two colleges with one group, course, academic item, and user in each. Assert a representative scoped to College A cannot retrieve or mutate College B's group, courses, items, or reminders through the group-scoped query and permission helpers. Assert a newly installed single-college pilot can resolve its seeded college.

Run: `python -m pytest tests/test_college_isolation.py -q`

Expected before implementation: tests fail because groups have no college owner and no college-scoped authorization helper exists.

- [ ] **Step 2: Add College and bind groups to exactly one college**

Add a `College` model with `id`, `name`, unique `slug`, `active`, and `created_at`. Add non-null `college_id` to `Group` with a foreign key and index. A migration must create a deterministic `pilot` college and associate existing groups with it before making the column non-null; it must not delete or reassign existing group records.

- [ ] **Step 3: Centralize college-scoped group lookup and authorization**

Add CRUD helpers that resolve a group only when both `chat_id` and `college_id` match. Update permission helpers to require the group/college context for representative or admin actions. Existing Telegram chat IDs remain globally unique, but no cross-college access should rely on that uniqueness as the authorization check.

- [ ] **Step 4: Verify isolation and migration backfill**

Run: `python -m pytest tests/test_college_isolation.py tests/test_migrations.py -q`

Expected: College A queries and actions cannot access College B records; existing groups receive the pilot college ID during upgrade.

## Task 5: Make Telegram message processing idempotent

**Files:**
- Modify: `app/database/models.py`
- Modify: `app/database/crud.py`
- Modify: `app/services/routing_service.py`
- Modify: `app/bot/handlers/group.py`
- Create: `migrations/versions/0003_message_receipts.py`
- Create: `tests/test_update_idempotency.py`

- [ ] **Step 1: Define regression tests using one Telegram source identity**

Create a registered group/topic/course and invoke `process_group_message` twice with the same `chat_id` and `message_id`. Assert there is one academic item, one set of reminders, and one acknowledgement. Invoke it with a different `message_id` and equivalent content to retain the existing semantic duplicate behavior.

Run: `python -m pytest tests/test_update_idempotency.py -q`

Expected before implementation: the repeated message creates duplicate acknowledgement or processing side effects, or the new receipt model is missing.

- [ ] **Step 2: Add a durable source-message receipt**

Add a `ProcessedMessage` table with `chat_id`, `message_id`, `status`, and timestamps. Add a unique constraint on `(chat_id, message_id)`. The key must use Telegram's chat and message identity, not message text, so two legitimate identical announcements remain distinguishable.

- [ ] **Step 3: Claim a message before extraction and item creation**

At the service boundary, insert and flush a receipt before processing. A unique-key conflict returns an already-processed result without repeating item creation, reminder creation, or acknowledgement. Keep the claim and academic item writes in one transaction. Do not swallow database errors unrelated to the unique conflict.

- [ ] **Step 4: Apply the receipt migration and verify retries**

Run: `python -m pytest tests/test_update_idempotency.py tests/test_services.py -q`

Expected: repeated Telegram delivery does not create a second academic item or reminder set; a new source message with similar text still follows semantic duplicate detection.

## Task 6: Make reminders recoverable from database state

**Files:**
- Modify: `app/database/models.py`
- Modify: `app/database/crud.py`
- Modify: `app/services/reminder_service.py`
- Modify: `app/reminders/scheduler.py`
- Create: `migrations/versions/0004_reminder_delivery_state.py`
- Create: `tests/test_reminder_recovery.py`

- [ ] **Step 1: Add reminder recovery tests**

Cover: restart rebuilds future reminders from database rows; a due reminder is dispatched by the recovery scan; cancelled/sent reminders are never dispatched; scheduling failure before transaction commit does not leave a phantom job; a failed Bot API send remains retryable; and two concurrent dispatch attempts cannot both claim the same reminder.

Run: `python -m pytest tests/test_reminder_recovery.py -q`

Expected before implementation: tests fail on overdue recovery, delivery-state visibility, or missing atomic claim behavior.

- [ ] **Step 2: Model reminder delivery state explicitly**

Add persisted states `pending`, `sending`, `sent`, `failed`, and `cancelled`, plus `attempt_count`, `last_error`, and `lease_until`. Preserve a migration mapping from existing `sent`/`cancelled` booleans so existing pilot records retain their meaning. Keep `sent_at` as the success timestamp.

- [ ] **Step 3: Make reminder rows authoritative and schedule after commit**

Do not add APScheduler jobs inside the database transaction that creates a reminder. Run a periodic due-reminder dispatcher that claims due rows atomically from PostgreSQL, and rebuild/dispatch due work on startup. Use APScheduler only to wake the dispatcher and retain the existing weekly digest jobs. Expired leases become retryable according to an explicit retry limit and backoff.

- [ ] **Step 4: Send outside the claim transaction and record the outcome**

Commit a claim before calling Telegram. Mark success with `sent_at` after Telegram confirms. On a definite Bot API failure, record a sanitized error type, increment attempts, and retry with backoff. Do not log message text, the bot token, or database credentials. An ambiguous network timeout after request transmission must be logged as ambiguous rather than claiming exactly-once delivery; Telegram sendMessage has no idempotency key, so the system cannot mathematically guarantee no duplicate in that crash window.

- [ ] **Step 5: Verify reminder recovery and delivery state**

Run: `python -m pytest tests/test_reminder_recovery.py tests/test_services.py -q`

Expected: only one concurrent worker can claim each reminder; due work is visible after restart; failed work remains recoverable; sent and cancelled work is excluded.

## Task 7: Improve webhook and health diagnostics

**Files:**
- Modify: `app/bot/webhook.py`
- Modify: `app/config.py`
- Modify: `tests/test_runtime_diagnostics.py`
- Modify: `docs/live-validation/academic-os-phase-1.md`

- [ ] **Step 1: Test liveness, readiness, and webhook secret validation**

Assert `/health` remains a lightweight liveness response; add `/ready` that checks database connectivity and reports a generic failure without exposing connection details; when webhook mode is enabled with a configured secret, reject requests with a missing or wrong Telegram secret header and accept a correct header.

Run: `python -m pytest tests/test_runtime_diagnostics.py -q`

Expected before implementation: readiness and webhook-secret cases fail or are absent.

- [ ] **Step 2: Implement readiness and request validation**

Add a `WEBHOOK_SECRET` setting and validate `X-Telegram-Bot-Api-Secret-Token` using constant-time comparison. Keep secret validation disabled only for explicit local polling mode. Database readiness runs `SELECT 1`; return no exception detail or credentials to callers.

- [ ] **Step 3: Verify runtime diagnostics**

Run: `python -m pytest tests/test_runtime_diagnostics.py -q`

Expected: liveness remains independent from database readiness, and webhook tests cover valid and invalid secrets.

## Task 8: Validate the Cloudflare Worker integration and full checkpoint

**Files:**
- Modify: `docs/live-validation/academic-os-phase-1.md`
- Create: `docs/hosting/free-pilot-compatibility.md`
- Create: `cloudflare/worker/migrations/0003_academic_reminders.sql`
- Create: `app/integrations/cloudflare_sync.py`

- [ ] **Step 1: Inventory runtime dependencies and hosting constraints**

Record which imported packages require native extensions, filesystem writes, long-lived event loops, direct TCP database connections, or APScheduler. Record whether the Cloudflare Python Workers build can import FastAPI, aiogram, SQLAlchemy, and the proposed D1 adapter, and whether a scheduled invocation can reach due reminders under the free plan.

- [ ] **Step 2: Keep or reject the Cloudflare candidate based on a reproducible proof**

Document exact build and smoke commands and their outputs. If the project cannot run the Telegram webhook and scheduled dispatcher on Workers without a broad rewrite or unsupported package, reject it for this pilot and retain a Python runtime with hosted PostgreSQL. No app-local database is permitted in either production option.

**Progress:** The local FastAPI + D1 + Cron proof passes, including migration and restart checks. A remote D1 database now has migrations `0001`–`0008`, and staging Worker `academic-assistant-college-pilot` is deployed with one-minute Cron. Remote `/health` and `/ready` pass; scheduled runs completed successfully. A synthetic webhook callback confirms wrong-secret rejection and valid-secret handling without a Telegram send. A live Cron test claimed one due reminder, attempted a deliberately invalid destination, and persisted the sanitized failed/retry state; the test row was removed. Staging secrets are stored in Cloudflare; the Telegram production webhook remains unset. The Worker has bounded request bodies, lease-based update idempotency, `/upcoming`, `/resources`, Addis Ababa reminders, weekly private digests, and college-scoped group/course/representative tables. College setup includes an authenticated provisioning endpoint, private use-limited invite codes, `/group_id`, `/group_setup`, forum-topic `/course_map`, group-admin rep controls, and permission checks for event creation/approval. Academic items and resources are stored against an active group and college; app-synced items are only accepted for registered groups. These flows have local tests. The app scheduler remains the active reminder sender until a deliberate cutover. Private quizzes/results, polls, and a college administration dashboard remain unported. No real group or academic data is in remote D1; successful Telegram delivery, real AI accuracy, and free-plan CPU usage remain unverified, so this checkpoint is still open.

- [ ] **Step 3: Run the regression suite for the complete checkpoint**

Run: `python -m pytest -q`

Expected: all existing and new tests pass. Report pre-existing failures separately; do not mark the checkpoint complete while any changed-path test fails.

- [ ] **Step 4: Complete real Telegram validation only against a confirmed test target**

Use a dedicated test supergroup/topic. Verify one assignment, one exam, duplicate update replay, source links, reminder recovery, reminder delivery, wrong webhook secret rejection, and restart behavior. Record the bot username, group, timestamps, and outcomes in `docs/live-validation/academic-os-phase-1.md`; never use a live college class group for test data.

## Later implementation plans

After checkpoint 1 is accepted, create separate plans for: (a) course resources and search plus weekly digests, (b) private quizzes plus class-representative controls and polls, and (c) college pilot readiness, backup/restore, and deployment. The Mini App remains a later project.

## Self-review

- **Spec coverage:** Checkpoint 1 requirements, college isolation, source context, duplicate handling, reminders, startup recovery, durable production DB, webhook security, health checks, and the live validation checklist map to Tasks 2–8. The new student features are intentionally assigned to later plans.
- **Hosting realism:** Cloudflare is the explicit target; the plan requires a direct D1 binding adapter, Workers-compatible Telegram transport, local Worker tests, and measurement of the free CPU limit before live use.
- **Delivery semantics:** The plan explicitly avoids an impossible exactly-once claim across Telegram's non-idempotent send API and an ambiguous network failure.
- **Placeholders:** No TBD/TODO tasks remain. Commands, files, conditions, and expected outcomes are named.
- **Type consistency:** Reminder state names, receipt identity, settings names, health paths, and migration sequence are consistent throughout this plan.
