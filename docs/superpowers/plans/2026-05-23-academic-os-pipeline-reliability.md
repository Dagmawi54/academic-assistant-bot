# Academic OS Pipeline Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make real Telegram forum-topic messages reliably become course-aware academic items, reminders, visible acknowledgements, and dashboard entries.

**Architecture:** Treat Telegram topic context as first-class academic context before AI extraction or routing. Split Academic OS extraction from `/ask` by introducing dedicated clients with separate configuration and rate limits. Add dashboard/service visibility for items, reminders, scheduler jobs, low-confidence items, duplicates, and target topic/course.

**Tech Stack:** Python, aiogram, SQLAlchemy async ORM, APScheduler, pytest, Groq/Gemini clients, SQLite/Postgres-compatible models.

---

### Task 1: Topic-Aware Intake Regression Tests

**Files:**
- Modify: `tests/test_services.py`
- Modify: `tests/conftest.py` only if bot/scheduler monkeypatching needs shared helpers.
- Exercise: `app.services.routing_service.process_group_message`

- [ ] **Step 1: Write failing test for “exam on monday” in a course topic**

Create a test that builds `Group`, active `Topic(topic_type="course", message_thread_id=42)`, and `Course(course_name="Math", topic_id=topic.id)`, monkeypatches `app.services.routing_service.bot.send_message`, calls `process_group_message(... thread_id=42, text="exam on monday" ...)`, and asserts one `AcademicItem` exists with `item_type == "exam"`, `course_id == course.id`, non-null `deadline`, source message link, and one visible acknowledgement sent to `message_thread_id=42`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests\test_services.py::test_topic_context_exam_creates_item_and_ack -q`

Expected: FAIL because the item course is not inferred from topic context.

- [ ] **Step 3: Write failing test for “assignment due friday” in a course topic**

Use the same setup with `Course(course_name="Data Structures")`, call `process_group_message(... text="assignment due friday" ...)`, and assert `AcademicItem.item_type == "assignment"`, course inferred from topic, reminders are persisted, and acknowledgement mentions assignment/course.

- [ ] **Step 4: Run test to verify it fails**

Run: `python -m pytest tests\test_services.py::test_topic_context_assignment_creates_item_reminders_and_ack -q`

Expected: FAIL because reminders or topic/course context are incomplete.

### Task 2: Coverage Classifier Regression Tests

**Files:**
- Modify: `tests/test_classifier.py`
- Exercise: `app.routing.classifier.classify`

- [ ] **Step 1: Write failing tests for coverage phrases**

Add tests for:

```python
def test_coverage_chapter_range():
    result = classify("covers chapter 1-4")
    assert result.message_type == "EXAM_COVERAGE"
    assert result.coverage is not None
    assert "1-4" in result.coverage

def test_coverage_exclusion():
    result = classify("excluding recursion")
    assert result.message_type == "EXAM_COVERAGE"
    assert result.coverage is not None
    assert "recursion" in result.coverage.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests\test_classifier.py::TestEntityExtraction::test_coverage_chapter_range tests\test_classifier.py::TestEntityExtraction::test_coverage_exclusion -q`

Expected: FAIL because current coverage extraction crashes or misses exclusion-only coverage.

### Task 3: Topic Context Resolver

**Files:**
- Create: `app/services/topic_context.py`
- Modify: `app/services/routing_service.py`
- Test: `tests/test_services.py`

- [ ] **Step 1: Implement a small resolver**

Create `resolve_topic_context(session, group_id, chat_id, thread_id)` returning a dataclass with `topic`, `course`, and `course_name`. It should only return active topics and active courses. It should log whether the topic was missing, closed, course-linked, or unlinked.

- [ ] **Step 2: Apply resolver before duplicate checks and item creation**

In `process_group_message`, resolve topic context immediately after group lookup. If `classification.course_hint` is empty and topic context has a course, merge the course name into the classification before destination resolution and item creation.

- [ ] **Step 3: Run topic tests**

Run: `python -m pytest tests\test_services.py::test_topic_context_exam_creates_item_and_ack tests\test_services.py::test_topic_context_assignment_creates_item_reminders_and_ack -q`

Expected: PASS.

### Task 4: Coverage Extraction Fix

**Files:**
- Modify: `app/routing/classifier.py`
- Test: `tests/test_classifier.py`

- [ ] **Step 1: Fix regex crash and support include/exclude coverage**

Replace the invalid `[\d\s,-&and]` character class with safe patterns. Add exclusion keywords: `excluding`, `except`, `not including`, `excluded`. Make `covers chapter 1-4`, `covers chapters 1-4`, `mid covers trees and graphs`, and `excluding recursion` classify as `EXAM_COVERAGE`.

- [ ] **Step 2: Preserve casual discussion behavior**

Ensure date words alone do not make casual questions like `Hey does anyone have the notes from today?` become assignments.

- [ ] **Step 3: Run classifier tests**

Run: `python -m pytest tests\test_classifier.py -q`

Expected: PASS.

### Task 5: Runtime Observability and Dashboard Queries

**Files:**
- Modify: `app/services/routing_service.py`
- Modify: `app/services/reminder_service.py`
- Modify: `app/reminders/scheduler.py`
- Modify: `app/services/event_service.py`
- Modify: `app/bot/handlers/events.py`
- Modify: `app/admin/menus.py`

- [ ] **Step 1: Add explicit structured logs**

Log these stages with stable event names: `academic_intake_received`, `topic_context_resolved`, `academic_classified`, `academic_ai_extraction_attempted`, `academic_item_created`, `academic_duplicate_suppressed`, `academic_ack_sent`, `academic_reminder_rows_created`, `academic_reminder_job_scheduled`, `academic_event_emitted`.

- [ ] **Step 2: Add dashboard scheduler job query**

Expose APScheduler jobs using a safe service helper returning job id, next run time, and reminder id when the id matches `reminder_{id}`.

- [ ] **Step 3: Expand Events menu**

Add a minimal “Scheduler Jobs” button and include target course/topic labels in existing Events views.

- [ ] **Step 4: Run dashboard/service tests**

Run: `python -m pytest tests\test_services.py -q`

Expected: PASS.

### Task 6: AI Separation Boundary

**Files:**
- Create: `app/ai/academic_extraction_client.py`
- Create: `app/ai/chatbot_client.py`
- Modify: `app/config.py`
- Modify: `app/services/routing_service.py`
- Modify: `app/bot/handlers/commands.py`
- Modify: `app/bot/handlers/group.py`

- [ ] **Step 1: Add separate settings**

Add optional env aliases for academic and chatbot models/limits/keys while preserving current `GROQ_API_KEY` and `GEMINI_API_KEY` as defaults.

- [ ] **Step 2: Move extraction AI calls**

Make Academic OS extraction use `academic_extraction_client`, with its own rate limiter and fallback path.

- [ ] **Step 3: Move `/ask` calls**

Make `/ask` use `chatbot_client`, with its own rate limiter and fallback path.

- [ ] **Step 4: Keep voice transcription operational**

Do not route transcription through chatbot quota. Keep it available for multi-modal intake, and document whether it uses academic or transcription-specific settings.

- [ ] **Step 5: Run full tests**

Run: `python -m pytest -q`

Expected: PASS.

### Task 7: Live Telegram Validation Checklist

**Files:**
- Create: `docs/live-validation/academic-os-phase-1.md`

- [ ] **Step 1: Document exact live checks**

Record real Telegram validation with:
- group id
- topic name
- message text
- detected item id
- inferred course
- reminder row ids
- APScheduler job ids
- acknowledgement screenshot or copied message text
- Events dashboard visibility

- [ ] **Step 2: Validate messages in real forum topics**

Send and verify:
- `exam on monday`
- `assignment due friday`
- `covers chapter 1-4`
- `excluding recursion`

- [ ] **Step 3: Validate reminder delivery**

Create at least one near-future test event with a reminder offset that fires during validation. Confirm the reminder is delivered into the correct `message_thread_id`.

