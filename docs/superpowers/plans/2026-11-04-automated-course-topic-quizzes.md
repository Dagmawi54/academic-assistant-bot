# Automated Course Topic Quizzes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate validated, semester-scoped course quizzes from mapped Telegram topic materials and publish them as native polls in each group's configured Quiz topic.

**Architecture:** Add semester offerings, extracted source text, target-topic settings, generation runs, and durable work/delivery outboxes to D1. The Worker ingests only mapped course-topic PDFs/images and explicitly marked text notes, uses Cloudflare Workers AI for Markdown conversion and question generation, and uses Cron to process bounded work and send native quiz polls with duplicate protection.

**Tech Stack:** Python Cloudflare Worker, FastAPI webhook, Cloudflare D1 migrations, Workers AI binding, Telegram Bot API.

---

### Task 1: Add durable quiz schema and AI binding

**Files:**
- Create `cloudflare/worker/migrations/0011_automated_topic_quizzes.sql`
- Modify `cloudflare/worker/wrangler.jsonc`
- Modify `cloudflare/worker/tests/test_quiz_service.py`

- [x] Add `academic_groups.current_semester`, `course_offerings`, `course_quiz_sources`, per-group `quiz_topic_settings`, daily AI budgets, and durable generation, question, and poll-delivery records with college/group scoping and uniqueness keys.
- [x] Configure the Workers AI binding as `AI` while preserving existing D1 and Cron bindings.
- [x] Verify migration applies from 0001 through 0011 on a fresh SQLite test database and retains existing quiz tables.

### Task 2: Add semester, course-offering, Quiz-topic, and source commands

**Files:**
- Modify `cloudflare/worker/src/worker.py`
- Modify `cloudflare/worker/src/quiz_service.py`
- Modify `cloudflare/worker/tests/test_quiz_service.py`

- [x] Parse `/semester_set <label>`, `/quiz_topic`, and `/quiz_source` replies with bounded validation.
- [x] Restrict semester/Quiz-topic administration to Telegram group admins; allow registered reps or admins to map course topics and mark text notes.
- [x] Activate a course offering on `/course_map`; tag ingested sources with the current semester and mapped course, while preserving non-course resource search behavior.
- [x] Ensure regular messages, unsupported media, and unmapped topics do not become quiz sources.

### Task 3: Implement bounded source extraction and validation

**Files:**
- Modify `cloudflare/worker/src/worker.py`
- Modify `cloudflare/worker/tests/test_quiz_service.py`

- [x] Claim one leased extraction job per Cron tick; resolve Telegram file metadata, enforce 20 MB maximum, download supported PDF/images, call Workers AI Markdown conversion, and persist sanitized bounded extracted text and checksum.
- [x] Reject empty/low-text/unsupported conversion results; cap extraction at 50,000 characters/file and keep binary bytes transient.
- [x] Verify retry limits, college/chat/course/semester/topic joins, duplicate source suppression, and caption/replied-note handling.

### Task 4: Generate and deliver scheduled native Quiz polls

**Files:**
- Modify `cloudflare/worker/src/quiz_service.py`
- Modify `cloudflare/worker/src/worker.py`
- Modify `cloudflare/worker/tests/test_quiz_service.py`

- [x] Add Addis-local schedule gates for every-other-day 10:00 course rotation and daily 16:00 next-day exam prep; exclude courses without a current offering or sufficient current-semester source text.
- [x] Generate structured questions with Workers AI, validate prompt/options/answer/source references, and persist a run and questions before sending.
- [x] Send the course/date header and each question via Telegram `sendPoll` with `type=quiz`, `is_anonymous=true`, and `message_thread_id` set to the registered Quiz topic.
- [x] Persist per-poll states; confirm completed sends, avoid replaying uncertain sends, and bound outbound sends to one per Cron tick.
- [x] Extend the scheduled handler to process extraction and quiz work within current Cron limits without changing existing reminder/digest behavior.

### Task 5: Document setup and operational behavior

**Files:**
- Modify `cloudflare/worker/README.md`
- Modify `docs/hosting/cloudflare-staging.md` if present

- [x] Document admin setup (`/semester_set`, `/course_map`, `/quiz_topic`), rep text-source marking, supported PDF/image behavior, AI binding, schedules, failure reporting, and the requirement for a dedicated test group before webhook cutover.

### Task 6: Validate locally and stage safely

**Files:**
- Run focused Worker tests and formatting/type checks as supported by the project.
- Apply migration 0011 to the configured staging D1 only after local validation.
- Deploy only the staging Worker; leave Telegram's production webhook unchanged.

- [x] Verify the fresh schema and schedule logic locally, including the every-other-day gate and exam-next-day selection.
- [x] Run focused and existing Worker tests and lint checks.
- [ ] Wrangler dry-run and staging health/readiness checks (not yet run; local Wrangler toolchain is unavailable).
- [ ] Perform staging deployment and migration if credentials remain available; report separately whether live Telegram delivery could be validated in an authorized test group.
