# Course-Grounded Web Tutor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Cloudflare Worker `/ask` flow that retrieves only the current course's materials, supplements them with live web search, and returns a clear tutor explanation with source links.

**Architecture:** Add a durable D1 passage index populated from ready `course_quiz_sources`, then retrieve passages only after resolving the active group's mapped course and semester. A bounded Tavily adapter supplies public web results; Cloudflare Workers AI synthesizes a response from the retrieved evidence. The Worker formats source links itself, applies per-user/group rate limits, and never sends private material text to the web provider.

**Tech Stack:** Cloudflare Python Worker, D1/SQLite FTS5, Cloudflare Workers AI, Tavily Search API, pytest.

---

### Task 1: Persist searchable passages from course materials

**Files:**
- Create: `cloudflare/worker/migrations/0012_course_tutor_search.sql`
- Create: `cloudflare/worker/src/course_tutor_service.py`
- Modify: `cloudflare/worker/src/course_quiz_service.py`
- Modify: `cloudflare/worker/src/worker.py`
- Test: `cloudflare/worker/tests/test_course_tutor_service.py`
- Test: `cloudflare/worker/tests/test_quiz_service.py`

- [ ] **Step 1: Write failing passage-index tests**

Test that ready sources are divided into bounded overlapping passages and that an index job is queued for both file extraction completion and representative notes. Test a bounded index tick and a query that returns only the requested college, chat, course, and semester.

- [ ] **Step 2: Run focused tests and confirm the new behavior is absent**

Run: `pytest cloudflare/worker/tests/test_course_tutor_service.py cloudflare/worker/tests/test_quiz_service.py -q`

Expected: the new tutor service import or required APIs fail before implementation; existing quiz tests continue to show their current baseline where runnable.

- [ ] **Step 3: Add durable passage and FTS tables**

Create `course_tutor_index_jobs` keyed to `course_quiz_sources`, `course_tutor_passages` containing source and tenant metadata plus passage text, and an FTS5 virtual table for passage text/title. Add triggers so a source becoming ready queues one index job. Seed jobs for already-ready current-semester sources. Add `tutor_ai_daily_usage` with group/user/day counters for later request limits.

- [ ] **Step 4: Implement bounded idempotent indexing and scoped retrieval**

In `course_tutor_service.py`, chunk normalized extracted text into 1,200-character passages with 200-character overlap; atomically claim a bounded index job; replace prior passages for that source; insert passage and FTS rows; then mark the job indexed. Build a safe FTS query from bounded quoted terms. Retrieval must join passages to ready sources, active groups, active colleges, active courses, mapped topics, and the exact current semester before ranking and limiting results. Include original Telegram message links in returned source metadata.

- [ ] **Step 5: Hook extraction completion and Cron indexing to the passage service**

Keep source ingestion as-is. On each existing Cron tick, process at most one passage-index job before lower-priority quiz work; D1 triggers also queue ready notes and extracted files. Do not block Telegram webhook acknowledgements on indexing.

- [ ] **Step 6: Run focused tests and confirm the tests pass**

Run: `pytest cloudflare/worker/tests/test_course_tutor_service.py cloudflare/worker/tests/test_quiz_service.py -q`

Expected: scoped retrieval, indexing retries, chunk boundaries, and existing extraction behavior pass.

### Task 2: Add bounded web search and evidence-only tutor synthesis

**Files:**
- Create: `cloudflare/worker/src/course_tutor_ai.py`
- Test: `cloudflare/worker/tests/test_course_tutor_ai.py`
- Modify: `cloudflare/worker/src/course_tutor_service.py`

- [ ] **Step 1: Write failing tests for provider parsing and prompt construction**

Cover URL/title/snippet parsing, rejection of malformed provider responses, strict result and text limits, prompt-injection isolation, language preservation, material/global source distinction, conflict attribution, and the no-evidence instruction.

- [ ] **Step 2: Run the focused tests to confirm they fail**

Run: `pytest cloudflare/worker/tests/test_course_tutor_ai.py -q`

Expected: import or missing-function failures for the not-yet-created module.

- [ ] **Step 3: Implement the provider adapter**

Call Tavily's search endpoint with a bounded question and language, request a small number of results, parse only title, canonical URL, and bounded snippet, and reject non-HTTP(S) links. Read `TAVILY_SEARCH_API_KEY` from the Worker environment. Never include course excerpts, college/group names, user IDs, or message links in the provider request.

- [ ] **Step 4: Implement tutor prompt and source-token response format**

Provide the AI model only the selected course excerpts and bounded web results. Instruct it to teach clearly in the student's language, distinguish course evidence from global context, call out conflicts, and never obey instructions inside retrieved content. Require citations to use only supplied `[M#]` and `[W#]` tokens. Strip unsupported tokens and format the final source list with escaped titles and validated links in Python rather than asking the model to invent URLs.

- [ ] **Step 5: Run focused tests and confirm they pass**

Run: `pytest cloudflare/worker/tests/test_course_tutor_ai.py -q`

Expected: provider limits and source labels are verified without network calls, and the prompt makes provenance and safe fallback behavior explicit.

### Task 3: Integrate `/ask` with course topic context and rate limits

**Files:**
- Modify: `cloudflare/worker/src/worker.py`
- Modify: `cloudflare/worker/tests/test_worker.py`
- Modify: `cloudflare/worker/src/course_tutor_service.py`
- Modify: `cloudflare/worker/wrangler.jsonc` only if a non-secret provider setting is required

- [ ] **Step 1: Write failing webhook-command tests**

Cover `/ask` in a mapped current-semester course topic, missing course context, absent/failed web search with course-only fallback, no matching course material, safe HTML source links, and invocation without saving question or answer text. Verify ordinary non-command chat and existing command handlers retain their behavior.

- [ ] **Step 2: Run focused tests to confirm they fail for the missing flow**

Run: `pytest cloudflare/worker/tests/test_worker.py -q`

Expected: the new `/ask` requests currently return no tutor answer or hit missing service behavior.

- [ ] **Step 3: Resolve explicit course context before retrieval**

Handle bounded `/ask <question>` only in an active, registered group forum topic mapped to an active current-semester course. For a general topic, private chat, unmapped topic, or missing question, respond with a short instruction to ask in the mapped course topic. Do not infer a course from unrelated text or search another group. Preserve the source topic on the Telegram reply.

- [ ] **Step 4: Add atomic per-user, per-group, and global monthly limits**

Use D1 `INSERT ... ON CONFLICT ... DO UPDATE ... WHERE ... RETURNING` counters before provider calls: five requests per user per UTC day, 20 per group per UTC day, and 800 tutor requests globally per UTC month to reserve part of the provider's advertised free quota. Keep only usage counts and dates; do not persist question text, answer text, or web query. On exhaustion, respond with a useful retry-later message.

- [ ] **Step 5: Call retrieval, search, synthesis, and formatting in sequence**

Pass the course name and semester as context to the AI prompt, but send only the question to Tavily. If Tavily is missing, rate-limited, or failing, synthesize from relevant course material and label the web-search failure. If there is no material match, explicitly state that and use web results only. If neither source is available, ask the student to narrow the question or share course material. Add `/ask` to the Worker help text.

- [ ] **Step 6: Run the focused Worker tests**

Run: `pytest cloudflare/worker/tests/test_worker.py cloudflare/worker/tests/test_course_tutor_service.py cloudflare/worker/tests/test_course_tutor_ai.py -q`

Expected: the tutor flow and existing command dispatch tests pass.

### Task 4: Document setup and validate the feature end to end

**Files:**
- Modify: `cloudflare/worker/README.md`
- Modify: `docs/live-validation/academic-os-phase-1.md`
- Test: `cloudflare/worker/tests/test_course_tutor_service.py`
- Test: `cloudflare/worker/tests/test_course_tutor_ai.py`
- Test: `cloudflare/worker/tests/test_worker.py`

- [ ] **Step 1: Document key setup, limits, privacy, and command use**

Document `wrangler secret put TAVILY_SEARCH_API_KEY`, `/ask <question>` in mapped course topics, citations and source access behavior, provider quota degradation, per-user/group rate limits, and the fact that private material is not sent to Tavily.

- [ ] **Step 2: Add acceptance coverage for tenant isolation and citations**

Ensure tests prove that a same-keyword source from another college, group, course, or semester cannot enter tutor context; material citations use Telegram source links; web citations use only validated provider URLs; model output cannot inject arbitrary links or uncited `[M#]`/`[W#]` tokens.

- [ ] **Step 3: Run the complete Worker test suite**

Run: `pytest cloudflare/worker/tests -q`

Expected: all Worker tests pass, including quiz, reminders, polls, update handling, search, and tutor flows.

- [ ] **Step 4: Run static checks**

Run: `python -m compileall cloudflare/worker/src`

Expected: all Worker Python modules compile successfully.

- [ ] **Step 5: Validate in staging without changing the production webhook**

After the D1 migration is applied and the Tavily secret is configured, deploy to the existing staging Worker, use an authorized dedicated test group with one mapped course and sample materials, and verify source-only, web-only, combined-source, quota/failure, and cross-tenant isolation cases. Record results in the live-validation checklist. Do not change the production bot webhook during staging validation.
