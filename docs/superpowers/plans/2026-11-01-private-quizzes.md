# Private Quizzes Implementation Plan

> **For agentic workers:** Inline execution is authorized in this conversation. Keep the poll feature in its own follow-up plan.

**Goal:** Let class reps publish course-topic quizzes that enrolled students take privately, with their own score kept private and group-visible results limited to aggregates.

**Architecture:** Add college-scoped D1 quiz, question, attempt, and answer tables. Keep the Telegram update dispatcher thin and place quiz parsing/storage/rendering in `cloudflare/worker/src/quiz_service.py`; class reps author quizzes in the mapped topic, while students start from a private deep link and answer with inline buttons. Require current Telegram membership in the source group before exposing quiz questions, and make each attempt/question answer idempotent.

**Tech Stack:** Cloudflare Python Workers, FastAPI, D1, Telegram Bot API `sendMessage` with inline keyboards, pytest.

---

### Task 1: Add college-scoped quiz storage and validation

**Files:**
- Create: `cloudflare/worker/migrations/0009_private_quizzes.sql`
- Create: `cloudflare/worker/src/quiz_service.py`
- Modify: `cloudflare/worker/tests/test_worker.py`

- [x] Add D1 tables for quiz sets, questions, attempts, and answers. Quiz creation and public result lookup are scoped through the registered active group and matching `college_id`.
- [x] Add parsers for `/quiz_create Title`, `/quiz_question QUIZ_ID | Question | A | B | C | D | 1-4 [| explanation]`, `/quiz_publish QUIZ_ID`, and `/quiz_results QUIZ_ID`.
- [x] Test malformed options, oversized fields, and invalid answer numbers.
- [x] Run the focused parser tests and Worker tests.

### Task 2: Implement rep-controlled quiz authoring and publishing

**Files:**
- Modify: `cloudflare/worker/src/quiz_service.py`
- Modify: `cloudflare/worker/src/worker.py`
- Modify: `cloudflare/worker/tests/test_worker.py`

- [x] Allow only a registered Telegram admin or assigned rep to create a quiz in a mapped forum topic, add up to 20 questions, and publish it after at least one valid question exists.
- [x] Persist the group's college and mapped course on the quiz; reject IDs that belong to another group.
- [x] Return a private `/start quiz_<id>` link using the configured bot username without exposing correct answers.
- [x] Test wrong-group quiz IDs, missing topic mappings, and publish-state transitions. Telegram role authorization is enforced through the existing registered-group admin/representative check.

### Task 3: Implement private student attempts and results

**Files:**
- Modify: `cloudflare/worker/src/quiz_service.py`
- Modify: `cloudflare/worker/src/worker.py`
- Modify: `cloudflare/worker/tests/test_worker.py`

- [x] Parse quiz deep links from private `/start` and require the user to be a current member of the quiz's Telegram group before returning quiz content.
- [x] Store one answer per attempt/question. Derive the next unanswered question from persisted answers so retries recover after an interrupted callback.
- [x] Accept answers only in the student's private bot chat and only for that attempt's next unanswered question.
- [x] Send the completed score only to that student. Group `/quiz_results` shows attempt count and aggregate percentage without identities or individual scores.
- [x] Test membership rejection, private quiz callbacks, duplicate answers, resume behavior, scoring, and aggregate-only results.

### Task 4: Verify deployment compatibility and document the live gate

**Files:**
- Modify: `cloudflare/worker/README.md`
- Modify: `docs/live-validation/academic-os-phase-1.md`

- [x] Run focused Worker tests, Ruff, migration application against an in-memory SQLite schema, and `wrangler deploy --dry-run`.
- [x] Apply migration `0009` to staging D1 and deploy the quiz feature without setting Telegram's production webhook.
- [x] Record that real quiz delivery still requires an authorized test group; no quiz content was sent to a group.

**Acceptance:** Registered class reps can publish quizzes in mapped topics; only members can take them privately; answers and scores stay private to each student; reps see only aggregates; quiz retries cannot duplicate answers; all queries remain group- and college-scoped.
